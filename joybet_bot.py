# -*- coding: utf-8 -*-
"""
bot.py — Joybet verilerini çekip Telegram grubuna gönderen bot.

GÜNÜN KOMBİNESİ  : JOYBET banner + her kombine AYRI premium görsel kart
                   (takım logoları + seçim + oran + toplam oran + %BONUS).
SÜPER LİG        : yalnızca yarının maçları, JOYBET markalı görsel kartlar
                   (logolar + tarih/saat + 1X2 + %15 BONUS).

Kullanım:
  python bot.py --print                # göndermeden önizle (kartlar preview/ klasörüne)
  python bot.py                        # bir kez çek ve gönder
  python bot.py --loop --interval 300  # her 300 sn'de bir tekrarla
  python bot.py --kombine / --superlig # tek bölüm
  python bot.py --get-chat-id          # sohbet kimliğini bul

Gereksinimler: pip install websockets requests pillow
"""

import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
import re
import sys
import time
from datetime import datetime

import requests

try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError:  # pragma: no cover
    HAS_PIL = False

from joybet import (
    JoybetClient,
    format_odd,
    format_expires,
    JoybetError,
    DEFAULT_DOMAIN,
    SUPER_LIG_COMPETITION_ID,
    IST,
)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

# Affiliate / yönlendirme linki (OYNA butonu)
AFFILIATE_URL = "https://www.jybtpr.link/affiliates/?btag=535329"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BANNER_PATH = os.path.join(BASE_DIR, "assets", "banner_kombine.jpg")
PREVIEW_DIR = os.path.join(BASE_DIR, "preview")

# Tek dosya derlemesinde (joybet_bot.py) otomatik doldurulan banner içeriği.
BANNER_B64 = ""

# Değişiklik takibi: aynı içerik tekrar gönderilmesin (saatlik/otomatik çalışma için)
STATE_PATH = os.path.join(BASE_DIR, "state.json")

TR_DAYS = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]

# ----------------------------------------------------------------------------
# Animasyonlu (custom) emoji — ID'si olmayanlar standart kalır.
# Yeni ID: @FIND_MY_ID_BOT'a animasyonlu emojiyi gönderip custom_emoji_id ekleyin.
# ----------------------------------------------------------------------------
ANIMATED_EMOJI = {
    "🚀": "5389102131527556772",
    "👍": "5368324170671202286",
    "❤️": "10002345",
}


def E(emoji: str) -> str:
    """Emojiyi animasyonlu custom emojiye çevirir (ID varsa), yoksa aynen bırakır."""
    eid = ANIMATED_EMOJI.get(emoji)
    if eid:
        return f'<tg-emoji emoji-id="{eid}">{emoji}</tg-emoji>'
    return emoji


def oyna_button(label: str) -> str:
    """Link emojili + animasyonlu doğal CTA satırı."""
    return (f'━━━━━━━━━━━━━━━━\n{E("🚀")} {E("🔗")} '
            f'<a href="{AFFILIATE_URL}">{esc(label)}</a> '
            f'{E("🔗")} {E("🚀")}')


# ----------------------------------------------------------------------------
# Değişiklik takibi (dedupe)
# ----------------------------------------------------------------------------
def _load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_state(state: dict) -> None:
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        print(f"[!] state kaydedilemedi: {exc}")


def _sig(data) -> str:
    """İçeriğin kısa parmak izi (aynı içerik => aynı sig)."""
    return hashlib.sha1(
        json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


# Doğal, birbirinden farklı CTA yazıları (her mesajda sırayla farklısı kullanılır)
CTA_TEMPLATES = [
    "HEMEN %BONUS% BONUSU AL & OYNA",
    "ŞİMDİ OYNA — %BONUS% BONUSU KAP",
    "%BONUS% BONUSLA HEMEN OYNA",
    "OYNA, %BONUS% BONUS KAZAN",
    "KUPONU YAP, %BONUS% BONUS CEPSİNDE",
    "%BONUS% BONUS SENİ BEKLİYOR — OYNA",
    "HEMEN OYNA & BONUSUNU AL",
    "ŞANSI YAKALA — HEMEN OYNA",
]


def cta_label(bonus=None, idx: int = 0) -> str:
    b = int(bonus) if bonus else 15
    tpl = CTA_TEMPLATES[idx % len(CTA_TEMPLATES)]
    return tpl.replace("%BONUS%", f"%{b}")


# ----------------------------------------------------------------------------
# Yapılandırma
# ----------------------------------------------------------------------------
def _load_env_file(path: str) -> dict:
    out = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _valid_value(v) -> bool:
    if v is None:
        return False
    s = str(v).strip()
    if not s:
        return False
    low = s.lower()
    return not any(x in low for x in ("example", "your_token", "your_", "buraya", "botfather"))


def load_config(args) -> dict:
    base = os.path.dirname(os.path.abspath(__file__))
    cfg = {"BOT_TOKEN": "6030866018:AAF1Z3vWRQSzin1w_X8PzMO_yganYyrbmZE",
           "CHAT_ID": "-1003879743469",
           "JOYBET_DOMAIN": DEFAULT_DOMAIN,
           "SUPERLIG_ID": SUPER_LIG_COMPETITION_ID}

    cfg_path = os.path.join(base, "config.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                for k, v in json.load(f).items():
                    if _valid_value(v):
                        cfg[k] = v
        except Exception as exc:  # noqa: BLE001
            print(f"[!] config.json okunamadı: {exc}")

    for k, v in _load_env_file(os.path.join(base, ".env")).items():
        if _valid_value(v):
            cfg[k] = v

    for key in ("BOT_TOKEN", "CHAT_ID", "JOYBET_DOMAIN", "SUPERLIG_ID"):
        if os.environ.get(key):
            cfg[key] = os.environ[key]

    if args.domain:
        cfg["JOYBET_DOMAIN"] = args.domain
    if args.chat_id:
        cfg["CHAT_ID"] = args.chat_id

    try:
        cfg["SUPERLIG_ID"] = int(cfg["SUPERLIG_ID"])
    except (TypeError, ValueError):
        cfg["SUPERLIG_ID"] = SUPER_LIG_COMPETITION_ID
    return cfg


# ----------------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------------
def telegram_call(token: str, method: str, **payload) -> dict:
    url = TELEGRAM_API.format(token=token, method=method)
    try:
        r = requests.post(url, json=payload, timeout=30)
        return r.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "description": str(exc)}


def send_message(token: str, chat_id: str, text: str) -> bool:
    resp = telegram_call(token, "sendMessage", chat_id=chat_id, text=text,
                         parse_mode="HTML", disable_web_page_preview=True)
    if not resp.get("ok"):
        resp = telegram_call(token, "sendMessage", chat_id=chat_id,
                             text=_plain(text), disable_web_page_preview=True)
    return bool(resp.get("ok"))


def send_photo(token: str, chat_id: str, path: str, caption: str | None = None) -> bool:
    url = TELEGRAM_API.format(token=token, method="sendPhoto")
    try:
        with open(path, "rb") as f:
            data = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption
                data["parse_mode"] = "HTML"
            r = requests.post(url, data=data, files={"photo": f}, timeout=60)
        return bool(r.json().get("ok"))
    except Exception as exc:  # noqa: BLE001
        print(f"[!] Fotoğraf gönderilemedi: {exc}")
        return False


def get_chat_id(token: str) -> None:
    resp = telegram_call(token, "getUpdates", offset=-10, timeout=5)
    if not resp.get("ok"):
        print("Hata:", resp)
        return
    seen = {}
    for u in resp.get("result", []):
        msg = u.get("message") or u.get("channel_post")
        if not msg:
            continue
        chat = msg.get("chat", {})
        cid = chat.get("id")
        title = chat.get("title") or chat.get("first_name") or chat.get("username") or ""
        seen[cid] = title
    if not seen:
        print("Henüz mesaj yok. Botun bulunduğu gruba bir mesaj yazın ve tekrar çalıştırın.")
    else:
        print("Bulunan sohbet kimlikleri:")
        for cid, title in seen.items():
            print(f"  CHAT_ID={cid}   ({title})")


# ----------------------------------------------------------------------------
# Yardımcılar
# ----------------------------------------------------------------------------
def _plain(s: str) -> str:
    s = re.sub(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>', r"\1", s)
    s = re.sub(r'<a href="[^"]*">(.*?)</a>', r"\1", s)
    for tag in ("b", "i", "code", "pre"):
        s = s.replace(f"<{tag}>", "").replace(f"</{tag}>", "")
    s = s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return s


def esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def match_datetime(start_ts) -> dict:
    if not start_ts:
        return {"date": "—", "day": "", "time": "—"}
    dt = datetime.fromtimestamp(int(start_ts), tz=IST)
    return {"date": dt.strftime("%d.%m.%Y"),
            "day": TR_DAYS[dt.weekday()],
            "time": dt.strftime("%H:%M")}


def ensure_banner() -> str | None:
    if os.path.exists(BANNER_PATH):
        return BANNER_PATH
    if BANNER_B64:
        try:
            os.makedirs(os.path.dirname(BANNER_PATH), exist_ok=True)
            with open(BANNER_PATH, "wb") as f:
                f.write(base64.b64decode(BANNER_B64))
            return BANNER_PATH
        except Exception as exc:  # noqa: BLE001
            print(f"[!] Banner oluşturulamadı: {exc}")
    return None


# ----------------------------------------------------------------------------
# Premium görsel üretimi (JOYBET markalı)
# ----------------------------------------------------------------------------
GOLD_LIGHT = (255, 236, 170)
GOLD_DARK = (196, 138, 24)


def _font(size: int, bold: bool = True):
    if not HAS_PIL:
        return None
    cands = ["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf",
             "C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/segoeui.ttf"]
    if not bold:
        cands = ["C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/segoeui.ttf"] + cands
    cands += ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    for c in cands:
        if os.path.exists(c):
            try:
                return ImageFont.truetype(c, size)
            except Exception:  # noqa: BLE001
                continue
    try:
        return ImageFont.load_default()
    except Exception:  # noqa: BLE001
        return None


def _vgrad(w: int, h: int, top=(14, 26, 52), bottom=(3, 8, 17)):
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        d.line([(0, y), (w, y)],
               fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return img.convert("RGBA")


def _glow(img, cx=0.5, cy=0.0, rx=950, ry=520, color=(212, 165, 60), alpha=30):
    w, h = img.size
    g = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gd = ImageDraw.Draw(g)
    gd.ellipse([cx * w - rx, cy * h - ry, cx * w + rx, cy * h + ry], fill=color + (alpha,))
    img.alpha_composite(g)


def _text(img, xy, text, font, fill, anchor="mm"):
    d = ImageDraw.Draw(img)
    d.text(xy, text, font=font, fill=fill, anchor=anchor)


def _gradient_text(img, xy, text, font, anchor="mm", c1=GOLD_LIGHT, c2=GOLD_DARK):
    """Altın degrade dolgulu metin (RGBA görsel üzerine)."""
    w, h = img.size
    mask = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(mask)
    md.text(xy, text, font=font, fill=255, anchor=anchor)
    bbox = mask.getbbox()
    if not bbox:
        return
    grad = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    y0, y1 = bbox[1], bbox[3]
    for y in range(y0, y1 + 1):
        t = (y - y0) / max(1, y1 - y0)
        col = (int(c1[0] + (c2[0] - c1[0]) * t),
               int(c1[1] + (c2[1] - c1[1]) * t),
               int(c1[2] + (c2[2] - c1[2]) * t), 255)
        gd.line([(bbox[0], y), (bbox[2], y)], fill=col)
    grad.putalpha(mask)
    img.alpha_composite(grad)


def _word_width(font, text, spacing=0):
    return sum(font.getlength(c) for c in text) + spacing * (len(text) - 1)


def _draw_word(img, cx, y, text, font, spacing=8, c1=GOLD_LIGHT, c2=GOLD_DARK):
    """Harf aralıklı altın degrade kelime."""
    widths = [font.getlength(c) for c in text]
    total = sum(widths) + spacing * (len(text) - 1)
    x = cx - total / 2
    for c, w in zip(text, widths):
        _gradient_text(img, (x + w / 2, y), c, font, "mm", c1, c2)
        x += w + spacing
    return total


def _gold_pill(img, cx, cy, w, h, text, font, text_color=(30, 20, 5, 255)):
    """Altın degrade dolgulu yuvarlak rozet."""
    x0, y0 = int(cx - w / 2), int(cy)
    pill = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    pd = ImageDraw.Draw(pill)
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w, h], radius=h // 2, fill=255)
    for i in range(h):
        t = i / h
        col = (int(GOLD_LIGHT[0] + (GOLD_DARK[0] - GOLD_LIGHT[0]) * t),
               int(GOLD_LIGHT[1] + (GOLD_DARK[1] - GOLD_LIGHT[1]) * t),
               int(GOLD_LIGHT[2] + (GOLD_DARK[2] - GOLD_LIGHT[2]) * t), 255)
        pd.line([(0, i), (w, i)], fill=col)
    pill.putalpha(mask)
    img.alpha_composite(pill, (x0, y0))
    _text(img, (cx, cy + h / 2), text, font, text_color)


def _brand_header(img, subtitle: str) -> int:
    """Belirgin JOYBET marka başlığı. Alt satırın başlangıç y koordinatını döndürür."""
    W = img.size[0]
    wf = _font(126, True)
    _draw_word(img, W / 2, 92, "JOYBET", wf, spacing=12)
    _text(img, (W / 2, 168), subtitle, _font(46, True), (235, 240, 250, 255))
    d = ImageDraw.Draw(img)
    d.line([(W * 0.22, 206), (W * 0.78, 206)], fill=(212, 165, 60, 120), width=2)
    return 224


def _frame(img):
    W, H = img.size
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([12, 12, W - 12, H - 12], radius=28,
                        outline=(212, 165, 60, 120), width=3)


LOGO_HOSTS = ["statistics.{domain}", "statistics.bcapps.org", "statistics.betconstruct.com"]
LOGO_SIZES = ["o", "b"]


def _download_logo(domain: str, team_id):
    """Takım logosunu birden çok sunucudan dener (yedekli). Başarısızsa None."""
    if not team_id or not HAS_PIL:
        return None
    try:
        bucket = int(team_id) // 2000
    except (TypeError, ValueError):
        return None
    last_err = ""
    for host_tpl in LOGO_HOSTS:
        host = host_tpl.format(domain=domain)
        for size in LOGO_SIZES:
            url = f"https://{host}/images/e/{size}/{bucket}/{team_id}.png"
            try:
                r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
                ctype = r.headers.get("content-type", "")
                if r.status_code == 200 and len(r.content) > 500 and ctype.startswith("image"):
                    return Image.open(io.BytesIO(r.content)).convert("RGBA")
                last_err = f"{host} HTTP {r.status_code} {ctype}"
            except Exception as exc:  # noqa: BLE001
                last_err = f"{host} -> {exc}"
    print(f"[logo] alınamadı team_id={team_id} ({last_err})")
    return None


def _paste_logo(card, logo, cx, cy, size, team_name):
    d = ImageDraw.Draw(card)
    r = size // 2
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, 255),
              outline=(212, 165, 60, 255), width=4)
    if logo is not None:
        logo = logo.convert("RGBA").resize((size - 22, size - 22), Image.LANCZOS)
        mask = logo.split()[3]
        card.paste(logo, (cx - (size - 22) // 2, cy - (size - 22) // 2), mask)
    else:
        initials = "".join(w[0] for w in str(team_name).split()[:2]).upper()
        f = _font(size // 2, True)
        if f:
            d.text((cx, cy), initials, font=f, fill=(16, 28, 52, 255), anchor="mm")


def _wrap(name, font, max_width):
    words = str(name).split()
    lines, cur = [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if not cur or font.getlength(t) <= max_width:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [str(name)]


def _truncate(s, font, max_width):
    if font.getlength(s) <= max_width:
        return s
    t = s
    while len(t) > 1 and font.getlength(t + "…") > max_width:
        t = t[:-1]
    return t + "…"


def _draw_name(img, name, x, cy, align, base_size, max_width, max_lines=2):
    """Takım adını, verilen max_width'e taşmadan çizer (gerektiğinde küçültür/kısaltır).

    Böylece uzun takım isimleri kartın ortasına taşıp diğer isimle çakışmaz.
    """
    f = None
    lines = []
    for size in range(base_size, max(base_size - 24, 12) - 1, -2):
        f = _font(size, True)
        if f is None:
            continue
        lines = _wrap(name, f, max_width)
        if len(lines) <= max_lines and all(f.getlength(l) <= max_width for l in lines):
            break
    else:
        f = _font(max(base_size - 24, 12), True) or _font(base_size, True)
        lines = _wrap(name, f, max_width)[:max_lines]
        if lines:
            lines[-1] = _truncate(lines[-1], f, max_width)
    if f is None:
        return
    anchor = "lm" if align == "left" else "rm"
    lh = f.size + 6
    y0 = cy - (lh * len(lines)) / 2 + lh / 2
    d = ImageDraw.Draw(img)
    for i, ln in enumerate(lines):
        d.text((x, y0 + i * lh), ln, font=f, fill=(255, 255, 255, 255), anchor=anchor)


# =============================================================================
# GÜNÜN KOMBİNESİ — kombinasyon kartı
# =============================================================================
def make_combo_card(combo: dict, domain: str, out_path: str) -> str | None:
    if not HAS_PIL:
        return None
    legs = combo.get("selections", [])
    n = len(legs)
    W = 1080
    row_h = 252
    header_h = 224
    combo_zone = 148
    footer_h = 150
    H = header_h + combo_zone + n * row_h + (n - 1) * 18 + 170 + footer_h

    img = _vgrad(W, H)
    _glow(img)
    _brand_header(img, "GÜNÜN KOMBİNESİ")

    # kombine adı + bonus
    _text(img, (W / 2, 262), (combo.get("name") or "").upper(),
          _font(56, True), (255, 255, 255, 255))
    bonus = combo.get("bonus")
    if bonus:
        _gold_pill(img, W / 2, 322, 340, 58, f"%{bonus:.0f} BONUS",
                   _font(36, True), (30, 20, 5, 255))

    pad = 46
    y = header_h + combo_zone
    for leg in legs:
        _combo_leg(img, leg, domain, pad, y, row_h)
        y += row_h + 18

    # toplam oran barı
    _total_bar(img, W / 2, y + 6, combo.get("total_odd"))

    # alt marka
    _gradient_text(img, (W / 2, H - 100), "JOYBET", _font(64, True), "mm")

    _frame(img)
    img.convert("RGB").save(out_path, quality=95)
    return out_path


def _combo_leg(img, leg, domain, x0, y, h):
    W = img.size[0]
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([x0, y, W - x0, y + h], radius=24,
                        fill=(12, 24, 48, 235), outline=(212, 165, 60, 90), width=2)
    cy = y + 80
    _paste_logo(img, _download_logo(domain, leg.get("team1_id")), 200, cy, 68, leg.get("team1", ""))
    _paste_logo(img, _download_logo(domain, leg.get("team2_id")), W - 200, cy, 68, leg.get("team2", ""))
    _draw_name(img, leg.get("team1", ""), 302, cy, "left", 40, 230)
    _draw_name(img, leg.get("team2", ""), W - 302, cy, "right", 40, 230)

    d.line([(x0 + 40, y + 152), (W - x0 - 40, y + 152)], fill=(212, 165, 60, 60), width=1)
    market = leg.get("market") or ""
    pick = leg.get("pick") or ""
    odd = format_odd(leg.get("odd"))
    line_y = y + 196
    mf, pf = _font(34, False), _font(42, True)
    head = f"{market}:  " if market else ""
    tail = f"{pick} @ {odd}"
    x = W / 2 - (mf.getlength(head) + pf.getlength(tail)) / 2
    if head:
        _text(img, (x, line_y), head, mf, (160, 180, 210, 255), "lm")
        x += mf.getlength(head)
    _gradient_text(img, (x, line_y), tail, pf, "lm")


def _total_bar(img, cx, cy, total):
    bw, bh = 560, 122
    x0, y0 = int(cx - bw / 2), int(cy)
    pill = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    pd = ImageDraw.Draw(pill)
    mask = Image.new("L", (bw, bh), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, bw, bh], radius=61, fill=255)
    for i in range(bh):
        t = i / bh
        col = (int(GOLD_LIGHT[0] + (GOLD_DARK[0] - GOLD_LIGHT[0]) * t),
               int(GOLD_LIGHT[1] + (GOLD_DARK[1] - GOLD_LIGHT[1]) * t),
               int(GOLD_LIGHT[2] + (GOLD_DARK[2] - GOLD_LIGHT[2]) * t), 255)
        pd.line([(0, i), (bw, i)], fill=col)
    pill.putalpha(mask)
    img.alpha_composite(pill, (x0, y0))
    ImageDraw.Draw(img).rounded_rectangle([x0, y0, x0 + bw, y0 + bh], radius=61,
                                          outline=(255, 220, 140, 255), width=3)
    _text(img, (cx, y0 + 32), "TOPLAM ORAN", _font(30, True), (60, 40, 10, 255))
    _text(img, (cx, y0 + 84), format_odd(total), _font(54, True), (20, 12, 3, 255))


def build_combo_caption(combo: dict, idx: int = 0) -> str:
    lines = [
        f"{E('🎯')} <b>GÜNÜN KOMBİNESİ</b>",
        f"{E('🏷')} {esc(combo.get('name', ''))}",
        f"{E('⏰')} Son Tarih: {format_expires(combo.get('expires'))}",
        f"{E('💰')} Toplam Oran: <b>{format_odd(combo.get('total_odd'))}</b>",
        "",
        oyna_button(cta_label(combo.get('bonus'), idx)),
    ]
    return "\n".join(lines)


def build_combo_text(combo: dict, idx: int = 0) -> str:
    """PIL yoksa metin yedeği."""
    lines = [f"🏷 <b>{esc(combo.get('name', ''))}</b>"]
    if combo.get("bonus"):
        lines.append(f"🎁 %{combo['bonus']:.0f} Bonus")
    lines.append(f"⏰ Son Tarih: {format_expires(combo.get('expires'))}")
    lines.append("")
    for s in combo.get("selections", []):
        lines.append(f"⚽️ {esc(s['team1'])} 🆚 {esc(s['team2'])}")
        lines.append(f"   ➜ {esc(s.get('market') or '')}: <b>{esc(s.get('pick') or '')} @ {format_odd(s['odd'])}</b>")
    lines.append("")
    lines.append(f"💰 <b>Toplam Oran: {format_odd(combo.get('total_odd'))}</b>")
    lines.append("")
    lines.append(oyna_button(cta_label(combo.get('bonus'), idx)))
    return "\n".join(lines)


# =============================================================================
# SÜPER LİG — maç kartı
# =============================================================================
def make_match_card(match: dict, league: str, domain: str, out_path: str) -> str | None:
    if not HAS_PIL:
        return None
    W, H = 1080, 1350
    img = _vgrad(W, H)
    _glow(img)
    _glow(img, cx=1.0, cy=1.0, rx=700, ry=500, color=(24, 200, 140), alpha=20)
    _brand_header(img, "SÜPER LİG")

    _text(img, (W / 2, 262), league.upper(), _font(44, True), (240, 205, 120, 255))
    _text(img, (W / 2, 310), "MAÇ ÖNCESİ", _font(30, False), (150, 170, 200, 255))

    t1, t2 = match.get("team1", ""), match.get("team2", "")
    logo1 = _download_logo(domain, match.get("team1_id"))
    logo2 = _download_logo(domain, match.get("team2_id"))
    _paste_logo(img, logo1, 290, 470, 125, t1)
    _paste_logo(img, logo2, W - 290, 470, 125, t2)

    _draw_name(img, t1, 290, 625, "left", 46, 240)
    _draw_name(img, t2, W - 290, 625, "right", 46, 240)

    dt = match_datetime(match.get("start_ts"))
    _text(img, (W / 2, 745), f"{dt['date']} {dt['day']}  •  {dt['time']}",
          _font(44, True), (235, 240, 250, 255))

    o = match.get("odds", {})
    for label, val, cx in (("1", o.get("1"), 250), ("X", o.get("X"), W // 2), ("2", o.get("2"), W - 250)):
        bw, bh = 230, 108
        bx0, by0 = cx - bw // 2, 820
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([bx0, by0, bx0 + bw, by0 + bh], radius=20,
                            fill=(10, 18, 36, 255), outline=(212, 165, 60, 255), width=3)
        _text(img, (cx, by0 + 30), label, _font(36, True), (150, 170, 200, 255))
        _text(img, (cx, by0 + 74), format_odd(val), _font(40, True), (255, 255, 255, 255))

    _gold_pill(img, W / 2, 1000, 430, 96, "%15 BONUS", _font(52, True), (30, 20, 5, 255))

    _gradient_text(img, (W / 2, H - 160), "JOYBET", _font(66, True), "mm")

    _frame(img)
    img.convert("RGB").save(out_path, quality=95)
    return out_path


def build_card_caption(match: dict, league: str, idx: int = 0) -> str:
    dt = match_datetime(match.get("start_ts"))
    lines = [
        f"{E('⚽️')} <b>{esc(match.get('team1', ''))} {E('🆚')} {esc(match.get('team2', ''))}</b>",
        f"{esc(league)} • {dt['date']} {dt['day']} • {dt['time']}",
        "",
        oyna_button(cta_label(15, idx)),
    ]
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Ana akış
# ----------------------------------------------------------------------------
async def fetch_all(cfg: dict, do_kombine: bool, do_superlig: bool) -> dict:
    out = {}
    async with JoybetClient(domain=cfg["JOYBET_DOMAIN"]) as client:
        if do_kombine:
            out["kombine"] = await client.get_kombine()
        if do_superlig:
            out["superlig"] = await client.get_superlig(cfg["SUPERLIG_ID"])
    return out


def run_once(cfg: dict, do_kombine: bool, do_superlig: bool, do_print: bool) -> bool:
    token = cfg.get("BOT_TOKEN", "")
    chat_id = str(cfg.get("CHAT_ID", ""))
    if not do_print and (not token or not chat_id):
        print("[!] BOT_TOKEN veya CHAT_ID tanımlı değil. Veriler ekrana yazdırılıyor.\n")
        do_print = True

    try:
        data = asyncio.run(fetch_all(cfg, do_kombine, do_superlig))
    except JoybetError as exc:
        print(f"[!] Veri çekilemedi: {exc}")
        return False

    # Teşhis: takım ID'leri geldi mi? (GitHub logunda görünür)
    if do_superlig:
        for m in data.get("superlig", {}).get("matches", []):
            print(f"[teşhis] {m.get('team1')} (id={m.get('team1_id')}) — "
                  f"{m.get('team2')} (id={m.get('team2_id')})")
    if do_kombine:
        for c in data.get("kombine", {}).get("combos", []):
            for s in c.get("selections", []):
                print(f"[teşhis] {s.get('team1')} (id={s.get('team1_id')}) — "
                      f"{s.get('team2')} (id={s.get('team2_id')})")

    ok = True
    os.makedirs(PREVIEW_DIR, exist_ok=True)
    cta_idx = 0  # her mesajda farklı (doğal) CTA yazısı için sayaç
    state = _load_state()

    # --- GÜNÜN KOMBİNESİ ---
    if do_kombine:
        combos = [c for c in data.get("kombine", {}).get("combos", []) if not c.get("dead")]
        if combos:
            sig = _sig([[c["name"], c["total_odd"],
                         [[s["team1"], s["team2"], s["market"], s["pick"], s["odd"]]
                          for s in c["selections"]]] for c in combos])
            if (not do_print) and state.get("combo_hash") == sig:
                print("[i] Kombine değişmedi, tekrar gönderilmedi.")
            else:
                banner = ensure_banner()
                if do_print:
                    if banner:
                        print(f"[BANNER FOTO] {banner}")
                    for i, c in enumerate(combos):
                        path = os.path.join(PREVIEW_DIR, f"combo_{i + 1}.png")
                        made = make_combo_card(c, cfg["JOYBET_DOMAIN"], path)
                        print(f"[KOMBİNE KART] {made or 'PIL yok'}")
                        print(_plain(build_combo_caption(c, cta_idx)) + "\n\n" + "=" * 40 + "\n")
                        cta_idx += 1
                else:
                    if banner:
                        ok &= send_photo(token, chat_id, banner)
                        time.sleep(0.4)
                    for i, c in enumerate(combos):
                        path = os.path.join(PREVIEW_DIR, f"combo_{i + 1}.png")
                        made = make_combo_card(c, cfg["JOYBET_DOMAIN"], path)
                        cap = build_combo_caption(c, cta_idx)
                        if made:
                            ok &= send_photo(token, chat_id, made, caption=cap)
                        else:
                            ok &= send_message(token, chat_id, build_combo_text(c, cta_idx))
                        cta_idx += 1
                        time.sleep(0.6)
                state["combo_hash"] = sig
        else:
            print("[i] Şu an için eksiksiz (aktif) bir Günün Kombinesi bulunamadı.")

    # --- SÜPER LİG ---
    if do_superlig:
        sdata = data.get("superlig", {})
        matches = sdata.get("matches", [])
        if matches:
            sig = _sig([[m["team1"], m["team2"], m["start_ts"], m["odds"]] for m in matches])
            if (not do_print) and state.get("superlig_hash") == sig:
                print("[i] Süper Lig maçları değişmedi, tekrar gönderilmedi.")
            else:
                league = sdata.get("competition", "Süper Lig")
                for i, m in enumerate(matches):
                    path = os.path.join(PREVIEW_DIR, f"card_{i + 1}.png")
                    made = make_match_card(m, league, cfg["JOYBET_DOMAIN"], path)
                    caption = build_card_caption(m, league, cta_idx)
                    if do_print:
                        print(f"[GÖRSEL KART] {made or 'PIL yok — kart üretilemedi'}")
                        print(_plain(caption) + "\n\n" + "─" * 40 + "\n")
                    else:
                        if made:
                            ok &= send_photo(token, chat_id, made, caption=caption)
                        else:
                            ok &= send_message(token, chat_id, caption)
                    cta_idx += 1
                    time.sleep(0.6)
                state["superlig_hash"] = sig
        else:
            print("[i] Yarın için Süper Lig maçı bulunamadı.")

    _save_state(state)
    return ok


def main():
    ap = argparse.ArgumentParser(description="Joybet -> Telegram botu")
    ap.add_argument("--domain", help="Site domaini (varsayılan: joybet794.pro)")
    ap.add_argument("--chat-id", help="Telegram chat_id (geçersiz kılar)")
    ap.add_argument("--kombine", action="store_true", help="Sadece Günün Kombinesi")
    ap.add_argument("--superlig", action="store_true", help="Sadece Süper Lig")
    ap.add_argument("--print", action="store_true", help="Telegram'a gönderme, ekrana yaz")
    ap.add_argument("--loop", action="store_true", help="Belirli aralıklarla tekrarla")
    ap.add_argument("--interval", type=int, default=300,
                    help="Tekrar aralığı (saniye, varsayılan 300)")
    ap.add_argument("--get-chat-id", action="store_true", help="getUpdates ile chat_id bul")
    args = ap.parse_args()

    cfg = load_config(args)

    if args.get_chat_id:
        if not cfg.get("BOT_TOKEN"):
            print("[!] Önce BOT_TOKEN tanımlayın.")
            sys.exit(1)
        get_chat_id(cfg["BOT_TOKEN"])
        return

    do_kombine = not args.superlig
    do_superlig = not args.kombine

    if args.loop:
        print(f"[i] Döngü başladı — her {args.interval} sn'de bir çekilecek. Ctrl+C ile durdurun.")
        while True:
            started = time.time()
            try:
                run_once(cfg, do_kombine, do_superlig, do_print=False)
            except Exception as exc:  # noqa: BLE001
                print(f"[!] Hata: {exc}")
            elapsed = time.time() - started
            time.sleep(max(5, args.interval - elapsed))
    else:
        ok = run_once(cfg, do_kombine, do_superlig, do_print=args.print)
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
