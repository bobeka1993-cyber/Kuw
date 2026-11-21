import logging
import re
import requests
import json
import os
import time
import math
import asyncio
import hashlib
from telegram import Update, MessageEntity, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          filters, ContextTypes, ConversationHandler, CallbackQueryHandler)
from telegram.request import HTTPXRequest

PROXY_URL = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_USERNAME = "@bobeka11"
CONFIG_FILE = os.environ.get("CONFIG_FILE", "config.json")

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)  # عشان التوكن ما يظهرش في اللوجات
logger = logging.getLogger(__name__)

ASK_CHANNEL = 0
ASK_RATE = 1
TATWEEL = '\u0640'

# pending_sessions: بيخزن كل الميديا والوصف لكل يوزر
# {chat_id: {"photos": [], "videos": [], "caption": "", "entities": [], "last_time": float}}
pending_sessions = {}

POST_TIMEOUT = 5  # ثواني - لو الفرق أكبر من كده يعتبره بوست جديد
DEBOUNCE_SECONDS = 1.5  # وقت الاستنى قبل النشر عشان نلحق نجمع كل عناصر الألبوم

# ============================================================
def load_config():
    if not os.path.exists(CONFIG_FILE):
        default = {"channel": "", "egp_to_kwd": 160.0}
        save_config(default)
        return default
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_config(data):
    folder = os.path.dirname(CONFIG_FILE)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def get_channel():
    return load_config().get("channel", "")

def get_queue_enabled():
    return bool(load_config().get("queue_enabled", False))

def set_queue_enabled(value):
    cfg = load_config()
    cfg["queue_enabled"] = bool(value)
    save_config(cfg)

def get_egp_to_kwd():
    return load_config().get("egp_to_kwd", 160.0)

def get_usd_per_kwd():
    try:
        r = requests.get("https://api.exchangerate-api.com/v4/latest/KWD", timeout=10)
        r.raise_for_status()
        return r.json()["rates"]["USD"]
    except Exception:
        pass
    try:
        r = requests.get("https://open.er-api.com/v6/latest/KWD", timeout=10)
        r.raise_for_status()
        return r.json()["rates"]["USD"]
    except Exception:
        pass
    return 3.25

def clean_line(line):
    return line.replace(TATWEEL, '')

def parse_number(s):
    s = s.strip().translate(str.maketrans('٠١٢٣٤٥٦٧٨٩٫٬،', '0123456789.,,'))
    s = s.rstrip('.,')
    if '.' in s and ',' in s:
        s = s.replace(',', '')
    elif ',' in s:
        parts = s.split(',')
        if len(parts) == 2 and len(parts[1]) == 3:
            s = s.replace(',', '')
        else:
            s = s.replace(',', '.')
    elif '.' in s:
        parts = s.split('.')
        if len(parts) == 2 and len(parts[1]) == 3:
            s = s.replace('.', '')
    return float(s)

# طريقة قراءة السعر (منقولة من main.py): العملة قبل الرقم أو بعده، أو كلمة "السعر/سعر/price"
_NUM = r'([0-9٠-٩][0-9٠-٩.,،٫٬]*)'
_USD_LABELS = (r'(?:\$|US\$|(?<![A-Za-z])USDT(?![A-Za-z])|(?<![A-Za-z])TETHER(?![A-Za-z])|'
               r'(?<![A-Za-z])USD(?![A-Za-z])|(?<![A-Za-z])US(?![A-Za-z])|U\.S\.|💵|💲|💸|💰|'
               r'دولار(?:\s+أمريكي)?|دولارات|تيثر)')
_EGP_LABELS = (r'(?:(?<![A-Za-z])EGP(?![A-Za-z])|(?<![A-Za-z])LE(?![A-Za-z])|جنيه(?:\s+مصري)?|جنيها|'
               r'ج\.?\s*م\.?|مصري|مصرى|(?<![ء-يA-Za-z])ج(?![ء-يA-Za-z]))')

def _label_patterns(labels):
    return [
        re.compile(rf'{labels}[ \t]*[:\-–—]*[ \t]*{_NUM}', re.IGNORECASE),
        re.compile(rf'{_NUM}[ \t]*[:\-–—]*[ \t]*{labels}', re.IGNORECASE),
    ]

USD_PATTERNS = _label_patterns(_USD_LABELS)
EGP_PATTERNS = _label_patterns(_EGP_LABELS)
PRICE_WORD_PATTERN = re.compile(r'(السعر|سعر|price)[ \t]*[:\-–—]*[ \t]*' + _NUM, re.IGNORECASE)

def _first_match_value(patterns, text):
    ms = [m for m in (p.search(text) for p in patterns) if m]
    if not ms:
        return None
    m = min(ms, key=lambda x: x.start())
    return next((g for g in m.groups() if g), None)

def find_price_in_line(line):
    """بترجع ("USD" أو "EGP", القيمة) أو None. نفس ترتيب main.py: دولار، جنيه، وبعدين كلمة السعر."""
    for cur, patterns in (("USD", USD_PATTERNS), ("EGP", EGP_PATTERNS)):
        val = _first_match_value(patterns, line)
        if val:
            try:
                return cur, parse_number(val)
            except Exception:
                pass
    m = PRICE_WORD_PATTERN.search(line)
    if m:
        try:
            return ("USD" if m.group(1).lower() == "price" else "EGP"), parse_number(m.group(2))
        except Exception:
            pass
    return None

# ============================================================
# أدوات لحساب المسافات بنظام UTF-16 (زي ما تليجرام بيحسب الـ entities)
# عشان نحافظ على الإيموجي المميز (Premium custom emoji) والفورمات الأصلية
# ============================================================
def utf16_len(s):
    return len(s.encode('utf-16-le')) // 2

def build_utf16_offset_map(text):
    """map[i] = موضع الحرف رقم i (بالـ python index) في نظام UTF-16"""
    offsets = [0]
    total = 0
    for ch in text:
        total += 2 if ord(ch) > 0xFFFF else 1
        offsets.append(total)
    return offsets

def entities_to_dicts(entities):
    if not entities:
        return []
    out = []
    for e in entities:
        out.append({
            "type": e.type,
            "offset": e.offset,
            "length": e.length,
            "custom_emoji_id": getattr(e, "custom_emoji_id", None),
            "url": getattr(e, "url", None),
            "language": getattr(e, "language", None),
        })
    return out

def dicts_to_entities(dicts):
    out = []
    for d in dicts:
        kwargs = {"type": d["type"], "offset": d["offset"], "length": d["length"]}
        if d.get("custom_emoji_id"):
            kwargs["custom_emoji_id"] = d["custom_emoji_id"]
        if d.get("url"):
            kwargs["url"] = d["url"]
        if d.get("language"):
            kwargs["language"] = d["language"]
        out.append(MessageEntity(**kwargs))
    return out

def apply_edits_to_entities(entity_dicts, edits_u16):
    """edits_u16: قائمة (start, end, new_text) بترتيب تصاعدي حسب start.
    بترجع entities معدّلة (الـ entities اللي بتتقاطع مع التعديل بيتشالها احتياطًا)."""
    edits_sorted = sorted(edits_u16, key=lambda x: x[0])
    result = []
    for e in entity_dicts:
        e_start = e["offset"]
        e_end = e["offset"] + e["length"]
        shift = 0
        dropped = False
        for (es, ee, new_text) in edits_sorted:
            old_len = ee - es
            new_len = utf16_len(new_text)
            delta = new_len - old_len
            if ee <= e_start:
                shift += delta
            elif es >= e_end:
                continue
            else:
                dropped = True
                break
        if not dropped:
            result.append({**e, "offset": e_start + shift})
    return result

def line_removal_ranges(indices, lines, line_char_starts):
    """بتاخد قائمة أرقام سطور (مرتبة) وبترجع نطاقات حذف بايثون (start_c, end_c)
    لكل مجموعة سطور متتالية، بحيث تتمسح مع سطر واحد زيادة (\\n) عشان
    ما يفضلش سطر فاضي مكانها."""
    if not indices:
        return []
    ranges = []
    run_start = indices[0]
    run_end = indices[0]
    for idx in indices[1:]:
        if idx == run_end + 1:
            run_end = idx
        else:
            ranges.append((run_start, run_end))
            run_start = idx
            run_end = idx
    ranges.append((run_start, run_end))

    result = []
    for (s_idx, e_idx) in ranges:
        if e_idx == len(lines) - 1 and s_idx > 0:
            # المجموعة وصلت لآخر سطر في الكابشن، فبنمسح الـ \n اللي قبلها
            start_c = line_char_starts[s_idx] - 1
            end_c = line_char_starts[e_idx] + len(lines[e_idx])
        else:
            start_c = line_char_starts[s_idx]
            end_c = line_char_starts[e_idx] + len(lines[e_idx]) + 1
        result.append((start_c, end_c))
    return result

def build_new_caption(caption, entities, egp_to_kwd, usd_per_kwd):
    """بيرجع (النص الجديد, قائمة entities جديدة) محافظ فيها على الإيموجي المميز
    والفورمات الأصلي، وبيضيف بولد لكل النص + سطري السعر + سطر التواصل."""
    entity_dicts = entities_to_dicts(entities)
    lines = caption.split("\n")

    # نحسب بداية كل سطر بالـ python index جوه النص الأصلي
    line_char_starts = []
    cursor = 0
    for line in lines:
        line_char_starts.append(cursor)
        cursor += len(line) + 1  # +1 للـ \n

    offset_map = build_utf16_offset_map(caption)

    edits = []  # (start_u16, end_u16, new_text)
    text_edits_char = []  # (start_c, end_c, new_text) - نفس التعديلات بس بمواضع بايثون

    # بندور على كل سطور الأسعار (جنيه أو دولار) في آخر 4 سطور بس، وبنجمعهم كل عملة لوحدها
    search_start = max(0, len(lines) - 4)
    matches_by_currency = {"EGP": [], "USD": []}  # currency -> [(line_idx, value), ...]

    for i in range(search_start, len(lines)):
        cl = clean_line(lines[i])
        found = find_price_in_line(cl)
        if found:
            matches_by_currency[found[0]].append((i, found[1]))

    # كل عملة موجودة (جنيه و/أو دولار) بتتحول لوحدها للدينار الكويتي + الدولار مع الربح.
    # لو نفس العملة اتكررت في أكتر من سطر، أول سطر بيتحول والباقي (أسعار قديمة) بيتمسح.
    for currency, matches in matches_by_currency.items():
        if not matches:
            continue
        price_line_idx, price_value = matches[0]
        if currency == "USD":
            kwd_val = math.ceil((price_value / usd_per_kwd) * 1.7)
        else:  # EGP
            kwd_val = math.ceil((price_value / egp_to_kwd) * 1.7)
        usd_val = round(kwd_val * usd_per_kwd, 1)

        line = lines[price_line_idx]
        start_c = line_char_starts[price_line_idx]
        end_c = start_c + len(line)
        new_price_text = f"السعر :: {kwd_val} دينار كويتي 🇰🇼\n𝑼𝑺𝑫𝑻 :: {usd_val} $ 🇺🇸"
        edits.append((offset_map[start_c], offset_map[end_c], new_price_text))
        text_edits_char.append((start_c, end_c, new_price_text))

        # أي سطور مكررة لنفس العملة في نفس النطاق (آخر 4 سطور) بتتمسح تمامًا
        old_indices = sorted(idx for (idx, _) in matches[1:])
        for (s_c, e_c) in line_removal_ranges(old_indices, lines, line_char_starts):
            edits.append((offset_map[s_c], offset_map[e_c], ""))
            text_edits_char.append((s_c, e_c, ""))

    contact_line = f"للتواصل : {OWNER_USERNAME} ✅"
    found_contact = False
    for i, line in enumerate(lines):
        if re.search(r"للتواصل", line):
            start_c = line_char_starts[i]
            end_c = start_c + len(line)
            edits.append((offset_map[start_c], offset_map[end_c], contact_line))
            text_edits_char.append((start_c, end_c, contact_line))
            found_contact = True
            break

    if not found_contact:
        end_c = len(caption)
        edits.append((offset_map[end_c], offset_map[end_c], f"\n{contact_line}"))
        text_edits_char.append((end_c, end_c, f"\n{contact_line}"))

    new_text = caption
    for (start_c, end_c, repl) in sorted(text_edits_char, key=lambda x: -x[0]):
        new_text = new_text[:start_c] + repl + new_text[end_c:]

    # نعدّل الـ entities الأصلية على أساس مواضع الـ UTF-16
    new_entity_dicts = apply_edits_to_entities(entity_dicts, edits)

    # نضيف بولد لكل النص الجديد
    new_entity_dicts.append({
        "type": "bold", "offset": 0, "length": utf16_len(new_text),
        "custom_emoji_id": None, "url": None, "language": None
    })

    return new_text, dicts_to_entities(new_entity_dicts)

def normalize_channel(text):
    text = text.strip()
    if text.startswith("https://t.me/"):
        text = "@" + text.replace("https://t.me/", "")
    elif text.startswith("t.me/"):
        text = "@" + text.replace("t.me/", "")
    elif not text.startswith("@"):
        text = "@" + text
    return text

def get_session(chat_id, reset=False):
    now = time.time()
    if chat_id in pending_sessions and not reset:
        session = pending_sessions[chat_id]
        # لو فات أكتر من POST_TIMEOUT ثانية من آخر رسالة، ابدأ session جديد
        if now - session.get("last_time", 0) > POST_TIMEOUT:
            pending_sessions[chat_id] = {"photos": [], "videos": [], "caption": "", "entities": [], "last_time": now}
    elif chat_id not in pending_sessions or reset:
        pending_sessions[chat_id] = {"photos": [], "videos": [], "caption": "", "entities": [], "last_time": now}
    else:
        pending_sessions[chat_id]["last_time"] = now
    return pending_sessions[chat_id]

# ============================================================
# منع التكرار: نفس الحساب (نفس الصورة/الفيديو أو نفس الوصف) ما يتنشرش تاني خلال 48 ساعة
# ============================================================
DEDUP_HOURS = 48
DEDUP_BY_MEDIA = True    # نفس الصورة أو الفيديو
DEDUP_BY_CAPTION = True  # نفس الوصف (من غير سطور السعر والتواصل)
PUBLISHED_FILE = os.path.join(os.path.dirname(CONFIG_FILE) or ".", "published.json")

def _load_published():
    try:
        with open(PUBLISHED_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_published(data):
    folder = os.path.dirname(PUBLISHED_FILE)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(PUBLISHED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)

def caption_fingerprint(caption):
    kept = []
    for line in clean_line(caption or "").split("\n"):
        if "للتواصل" in line or find_price_in_line(line):
            continue
        kept.append(line)
    text = re.sub(r"\s+", " ", " ".join(kept)).strip().lower()
    return ("c:" + hashlib.md5(text.encode("utf-8")).hexdigest()) if text else None

def dedup_keys(session):
    keys = []
    if DEDUP_BY_MEDIA:
        keys += ["m:" + u for u in session.get("uids", [])]
    if DEDUP_BY_CAPTION:
        fp = caption_fingerprint(session.get("caption", ""))
        if fp:
            keys.append(fp)
    return keys

def check_and_mark_published(keys):
    """True = الحساب جديد (واتسجل)، False = اتنشر قبل كده خلال المدة."""
    now = time.time()
    data = {k: v for k, v in _load_published().items() if now - v < DEDUP_HOURS * 3600}
    if any(k in data for k in keys):
        _save_published(data)
        return False
    for k in keys:
        data[k] = now
    _save_published(data)
    return True

def forget_published(keys):
    data = _load_published()
    for k in keys:
        data.pop(k, None)
    _save_published(data)

async def schedule_publish(context, chat_id, generation):
    """بينتظر شوية عشان يلحق يجمع كل عناصر الألبوم (صور+فيديو) قبل النشر.
    بيستخدم رقم "جيل" عشان يضمن إن نشر واحد بس هو اللي بيحصل، حتى لو
    الصورة والفيديو وصلوا قريبين جدًا من بعض."""
    await asyncio.sleep(DEBOUNCE_SECONDS)
    session = pending_sessions.get(chat_id)
    if not session:
        return
    # لو في تحديث أحدث وصل بعد ما الـ task ده اتعمل، سيبها للـ task الأحدث
    if session.get("generation") != generation:
        return
    if (session["photos"] or session["videos"]) and session["caption"]:
        # نمسح الجلسة فورًا (قبل أي await) عشان نضمن إن مفيش task تاني
        # ممكن يعدّي نفس الشرط وينشر مرة تانية
        pending_sessions.pop(chat_id, None)
        await submit_publish(context, chat_id, session)

# ============================================================
# الطابور (بيتشغّل ويتقفل من زرار في /menu): كل حساب بيتنشر بعد اللي قبله بدقيقة
# ============================================================
publish_queue = asyncio.Queue()
QUEUE_INTERVAL_SECONDS = 60  # المدة بين نشر حساب واللي بعده

async def submit_publish(context, chat_id, session):
    """لو الطابور شغال الحساب بيتضاف له، ولو مقفول بيتنشر فورًا."""
    if get_queue_enabled():
        await enqueue_publish(context, chat_id, session)
    else:
        await do_publish(context, chat_id, session)

async def enqueue_publish(context, chat_id, session):
    position = publish_queue.qsize() + 1
    await publish_queue.put((chat_id, session))
    if position == 1:
        await context.bot.send_message(chat_id=chat_id, text="✅ تم الاستلام، جاري النشر الآن...")
    else:
        wait_minutes = position - 1
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"✅ تم إضافة الحساب للطابور (رقم {position})\n⏳ هيتنشر تقريبًا بعد {wait_minutes} دقيقة"
        )

async def queue_worker(app):
    """شغال طول الوقت في الخلفية: بيسحب حساب من الطابور وينشره وبعدين يستنى دقيقة."""
    while True:
        chat_id, session = await publish_queue.get()
        try:
            await do_publish(app, chat_id, session)
        except Exception as e:
            logger.error(f"Queue worker error: {e}")
        finally:
            publish_queue.task_done()
        await asyncio.sleep(QUEUE_INTERVAL_SECONDS)

async def post_init(app):
    asyncio.create_task(queue_worker(app))

def menu_content():
    enabled = get_queue_enabled()
    status = "🟢 شغال (حساب كل دقيقة)" if enabled else "🔴 مقفول (النشر فوري)"
    text = f"⚙️ الإعدادات\n\n📬 الطابور: {status}\n⏳ مستني في الطابور: {publish_queue.qsize()}"
    label = "⏹ إيقاف الطابور" if enabled else "▶️ تشغيل الطابور"
    return text, InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data="toggle_queue")]])

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, keyboard = menu_content()
    await update.message.reply_text(text, reply_markup=keyboard)

async def toggle_queue_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    new_state = not get_queue_enabled()
    set_queue_enabled(new_state)
    await query.answer("✅ الطابور اتشغّل" if new_state else "⏹ الطابور اتقفل")
    text, keyboard = menu_content()
    await query.edit_message_text(text, reply_markup=keyboard)

# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channel = get_channel()
    egp_to_kwd = get_egp_to_kwd()
    usd_per_kwd = get_usd_per_kwd()

    if not channel:
        await update.message.reply_text("أهلاً! 👋\n\nابعتلي يوزر قناتك:")
        return ASK_CHANNEL

    await update.message.reply_text(
        f"✅ البوت شغال!\n\n"
        f"📢 القناة: {channel}\n"
        f"💱 سعر الدينار: {egp_to_kwd} جنيه\n"
        f"💵 1 KWD = {round(usd_per_kwd, 3)} USD\n\n"
        f"ابعتلي الميديا والوصف وأنا هتولى الباقي 🚀\n\n"
        f"/setrate - تغيير سعر الدينار\n"
        f"/setchannel - تغيير القناة\n"
        f"/rate - عرض الأسعار\n"
        f"/publish - نشر ما تم استلامه"
    )
    return ConversationHandler.END

async def got_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channel = normalize_channel(update.message.text)
    cfg = load_config()
    cfg["channel"] = channel
    save_config(cfg)
    await update.message.reply_text(f"✅ تم حفظ القناة: {channel}\n\nابعتلي سعر الدينار بالجنيه (مثال: 160):")
    return ASK_RATE

async def got_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rate = float(update.message.text.strip().replace(',', '.'))
        cfg = load_config()
        cfg["egp_to_kwd"] = rate
        save_config(cfg)
        await update.message.reply_text(f"✅ تم! 1 دينار = {rate} جنيه\n\nابعتلي المنشور 🚀")
    except Exception:
        await update.message.reply_text("❌ رقم غلط، مثال: 160")
        return ASK_RATE
    return ConversationHandler.END

async def setrate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(f"💱 السعر الحالي: 1 دينار = {get_egp_to_kwd()} جنيه\n\nلتغييره: /setrate 160")
        return
    try:
        rate = float(context.args[0].replace(',', '.'))
        cfg = load_config()
        cfg["egp_to_kwd"] = rate
        save_config(cfg)
        await update.message.reply_text(f"✅ تم التحديث: 1 دينار = {rate} جنيه")
    except Exception:
        await update.message.reply_text("❌ خطأ. مثال: /setrate 160")

async def setchannel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(f"📢 القناة: {get_channel()}\n\nلتغييرها: /setchannel @mychannel")
        return
    channel = normalize_channel(context.args[0])
    cfg = load_config()
    cfg["channel"] = channel
    save_config(cfg)
    await update.message.reply_text(f"✅ تم تغيير القناة: {channel}")

async def rate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    egp_to_kwd = get_egp_to_kwd()
    usd_per_kwd = get_usd_per_kwd()
    await update.message.reply_text(
        f"💱 الأسعار:\n🇰🇼 1 دينار = {egp_to_kwd} جنيه\n💵 1 KWD = {round(usd_per_kwd, 3)} USD"
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("تم الإلغاء.")
    return ConversationHandler.END

# ============================================================
async def publish_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ينشر كل حاجة متخزنة دلوقتي"""
    chat_id = update.message.chat_id
    session = get_session(chat_id)
    if not session["photos"] and not session["videos"]:
        await update.message.reply_text("❌ مفيش ميديا متخزنة. ابعت صور أو فيديوهات الأول.")
        return
    if not session["caption"]:
        await update.message.reply_text("❌ مفيش وصف. ابعت الوصف الأول.")
        return
    session = pending_sessions.pop(chat_id, None)
    await submit_publish(context, chat_id, session)

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channel = get_channel()
    if not channel:
        await update.message.reply_text("ابعتلي القناة الأول بـ /start")
        return

    message = update.message
    chat_id = message.chat_id
    session = get_session(chat_id)
    session["last_time"] = time.time()

    caption = message.caption or ""

    if message.photo:
        session["photos"].append(message.photo[-1].file_id)
        session.setdefault("uids", []).append(message.photo[-1].file_unique_id)
    elif message.video:
        session["videos"].append(message.video.file_id)
        session.setdefault("uids", []).append(message.video.file_unique_id)

    # لو في وصف مع الميديا خزّنه (مع الـ entities عشان الإيموجي المميز)
    if caption.strip():
        session["caption"] = caption
        session["entities"] = list(message.caption_entities or [])

    # لو عندنا ميديا ووصف، هنستنى شوية عشان نلحق نجمع باقي عناصر الألبوم بعدين ننشر
    if (session["photos"] or session["videos"]) and session["caption"]:
        session["generation"] = session.get("generation", 0) + 1
        asyncio.create_task(schedule_publish(context, chat_id, session["generation"]))
    else:
        # رد بحالة الاستقبال
        photos_count = len(session["photos"])
        videos_count = len(session["videos"])
        has_caption = "✅" if session["caption"] else "❌"
        await message.reply_text(
            f"✅ تم الاستلام\n\n"
            f"🖼️ صور: {photos_count}\n"
            f"🎥 فيديوهات: {videos_count}\n"
            f"📝 وصف: {has_caption}\n\n"
            f"{'ابعت الوصف 📝' if not session['caption'] else 'ابعت ميديا تانية أو /publish للنشر'}"
        )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channel = get_channel()
    if not channel:
        await update.message.reply_text("ابعتلي القناة الأول بـ /start")
        return

    chat_id = update.message.chat_id
    text = update.message.text or ""
    session = get_session(chat_id)
    session["last_time"] = time.time()
    session["caption"] = text
    session["entities"] = list(update.message.entities or [])

    if session["photos"] or session["videos"]:
        # عندنا ميديا خزّناها قبل كده، ممكن يكون لسه في صور/فيديوهات جاية
        # فبنستنى شوية (زي الميديا بالظبط) قبل ما ننشر عشان محدش يتقطع
        session["generation"] = session.get("generation", 0) + 1
        asyncio.create_task(schedule_publish(context, chat_id, session["generation"]))
    else:
        await update.message.reply_text("✅ تم حفظ الوصف\nدلوقتي ابعتلي الصور أو الفيديوهات 🖼️")

# ============================================================
async def do_publish(context, chat_id, session):
    channel = get_channel()
    egp_to_kwd = get_egp_to_kwd()
    usd_per_kwd = get_usd_per_kwd()

    photos = session["photos"]
    videos = session["videos"]
    caption = session["caption"]
    entities = session.get("entities") or []

    keys = dedup_keys(session)
    if keys and not check_and_mark_published(keys):
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🚫 الحساب ده اتنشر قبل كده خلال آخر {DEDUP_HOURS} ساعة، فما اتنشرش تاني."
        )
        return

    new_caption, new_entities = build_new_caption(caption, entities, egp_to_kwd, usd_per_kwd)

    try:
        await context.bot.send_message(chat_id=chat_id, text="⏳ جاري النشر...")

        # لو في صور بس
        if photos and not videos:
            if len(photos) == 1:
                await context.bot.send_photo(
                    chat_id=channel,
                    photo=photos[0],
                    caption=new_caption,
                    caption_entities=new_entities
                )
            else:
                # أرسل أول صورة مع الوصف
                await context.bot.send_photo(
                    chat_id=channel,
                    photo=photos[0],
                    caption=new_caption,
                    caption_entities=new_entities
                )
                # باقي الصور بدون وصف
                for photo_id in photos[1:]:
                    await context.bot.send_photo(chat_id=channel, photo=photo_id)

        # لو في فيديوهات بس
        elif videos and not photos:
            if len(videos) == 1:
                await context.bot.send_video(
                    chat_id=channel,
                    video=videos[0],
                    caption=new_caption,
                    caption_entities=new_entities
                )
            else:
                # أرسل أول فيديو مع الوصف
                await context.bot.send_video(
                    chat_id=channel,
                    video=videos[0],
                    caption=new_caption,
                    caption_entities=new_entities
                )
                # باقي الفيديوهات بدون وصف
                for video_id in videos[1:]:
                    await context.bot.send_video(chat_id=channel, video=video_id)

        # لو في صور وفيديوهات مع بعض
        elif photos and videos:
            # الصور أولاً بدون وصف
            for photo_id in photos:
                await context.bot.send_photo(chat_id=channel, photo=photo_id)
            # الفيديو الأول مع الوصف
            await context.bot.send_video(
                chat_id=channel,
                video=videos[0],
                caption=new_caption,
                caption_entities=new_entities
            )
            # باقي الفيديوهات
            for video_id in videos[1:]:
                await context.bot.send_video(chat_id=channel, video=video_id)

        await context.bot.send_message(chat_id=chat_id, text="✅ تم النشر! 🎉")

    except Exception as e:
        logger.error(f"Publish error: {e}")
        if keys:
            forget_published(keys)
        await context.bot.send_message(chat_id=chat_id, text=f"⚠️ مشكلة في النشر:\n{str(e)}")

# ============================================================
def main():
    request = HTTPXRequest(
        connect_timeout=30,
        read_timeout=30,
        connection_pool_size=8,
        proxy=PROXY_URL if PROXY_URL else None
    )
    app = Application.builder().token(BOT_TOKEN).request(request).get_updates_request(
        HTTPXRequest(
            connect_timeout=30,
            read_timeout=30,
            connection_pool_size=4,
            proxy=PROXY_URL if PROXY_URL else None
        )
    ).post_init(post_init).build()

    setup_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_channel)],
            ASK_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_rate)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    app.add_handler(setup_conv)
    app.add_handler(CommandHandler("setrate", setrate_command))
    app.add_handler(CommandHandler("setchannel", setchannel_command))
    app.add_handler(CommandHandler("rate", rate_command))
    app.add_handler(CommandHandler("publish", publish_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CallbackQueryHandler(toggle_queue_callback, pattern="^toggle_queue$"))
    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO, handle_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("بوت الكويت شغال ✅")
    app.run_polling(drop_pending_updates=True, timeout=30)

if __name__ == "__main__":
    main()
