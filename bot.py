from __future__ import annotations

import html
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote, urlparse

import requests
import telebot
from aliexpress_api import AliexpressApi, models
from dotenv import load_dotenv

# --------------------------------------------------------------------------
# الإعدادات
# --------------------------------------------------------------------------
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("bot")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_KEY = os.getenv("ALIEXPRESS_API_KEY")
API_SECRET = os.getenv("ALIEXPRESS_API_SECRET")
TRACKING_ID = os.getenv("ALIEXPRESS_TRACKING_ID")

missing = [n for n, v in [
    ("TELEGRAM_BOT_TOKEN", TOKEN),
    ("ALIEXPRESS_API_KEY", API_KEY),
    ("ALIEXPRESS_API_SECRET", API_SECRET),
    ("ALIEXPRESS_TRACKING_ID", TRACKING_ID),
] if not v]
if missing:
    raise SystemExit(f"Missing environment variables: {', '.join(missing)}")

# --------------------------------------------------------------------------
# قوالب الروابط  (غير مؤكدة - جرّبها وعدّلها إذا لزم)
# {id} = رقم المنتج.
# كل قالب يُمرَّر لاحقًا إلى get_affiliate_links ليتحول إلى رابط إحالة.
# --------------------------------------------------------------------------
# صيغ رابط العملات: مأخوذة من صفحة العملات الحقيقية التي ظهرت في سلسلة تحويل
# رابط فعلي (m.aliexpress.com/p/coin-index). تُجرَّب بالترتيب ويُستخدم أول ما ينجح.
COIN_URL_TEMPLATES = (
    "https://m.aliexpress.com/p/coin-index/index.html"
    "?_immersiveMode=true&tabname=configTab_1926001&productIds={id}",
    "https://m.aliexpress.com/p/coin-index/index.html"
    "?_immersiveMode=true&productIds={id}",
)
# إذا فشلت كل صيغ العملات: رابط إحالة عادي للمنتج (قد لا يفتح صفحة العملات)
PLAIN_URL_TEMPLATE = "https://www.aliexpress.com/item/{id}.html"

BUNDLE_URL_TEMPLATE = (
    "https://www.aliexpress.com/ssr/300000512/BundleDeals2"
    "?disableNav=YES&pha_manifest=ssr&_immersiveMode=true&productIds={id}"
)

LABEL_COIN = "🪙 <b>رابط العملات:</b>"
LABEL_BUNDLE = "📦 <b>رابط الباندل:</b>"
LABEL_FALLBACK = "🔗 <b>رابط المنتج (تعذر إنشاء رابط العملات):</b>"

RETRY_ATTEMPTS = 2
RETRY_DELAY = 0.5

# AliExpress يرفض الاستدعاءات المتقاربة (Api access frequency exceeds the limit)،
# لذلك نفصل بين كل استدعاءين بهذا الحد الأدنى (بالثواني). يمكنك تجربة تقليله.
API_MIN_GAP = 1.0

bot = telebot.TeleBot(TOKEN)

api = AliexpressApi(
    key=API_KEY,
    secret=API_SECRET,
    language=models.Language.EN,
    currency=models.Currency.USD,
    tracking_id=TRACKING_ID,
)

session = requests.Session()
session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
})

# --------------------------------------------------------------------------
# استدعاءات الـ API: واحد في كل مرة وبينها فاصل زمني
# --------------------------------------------------------------------------
_api_lock = threading.Lock()
_last_call_end = 0.0


def call_api(fn, *args, **kwargs):
    global _last_call_end
    with _api_lock:
        wait = API_MIN_GAP - (time.monotonic() - _last_call_end)
        if wait > 0:
            time.sleep(wait)
        try:
            return fn(*args, **kwargs)
        finally:
            _last_call_end = time.monotonic()


# --------------------------------------------------------------------------
# دوال مساعدة
# --------------------------------------------------------------------------
URL_RE = re.compile(r"https?://[^\s)\]>\"'<]+")
ALI_DOMAINS = ("aliexpress.com", "aliexpress.us", "ali.ski", "a.aliexpress.com")

# أنماط صارمة لرقم المنتج (آمنة)، وأنماط عامة تُستخدم كآخر خيار
STRICT_ID_PATTERNS = (
    r"/(?:item|i)/(\d{8,})",
    r"productIds?=(\d{8,})",
)
LOOSE_ID_PATTERNS = (
    r"[?&]id=(\d{8,})",
    r"(\d{13,})",
)


def find_urls(text: str) -> list[str]:
    return URL_RE.findall(text or "")


def is_aliexpress(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in ALI_DOMAINS)


def is_bundle_url(*urls: str) -> bool:
    """هل أحد الروابط يشير إلى صفحة عروض الباندل؟"""
    for u in urls:
        low = unquote(u or "").lower()
        if "bundledeals" in low or "bundle_deals" in low or "channel=bundle" in low:
            return True
    return False


def extract_product_id(text: str, loose: bool = False) -> str | None:
    patterns = STRICT_ID_PATTERNS + (LOOSE_ID_PATTERNS if loose else ())
    for pattern in patterns:
        m = re.search(pattern, text or "")
        if m:
            return m.group(1)
    return None


def _decode(text: str) -> str:
    """يفك ترميز الروابط والـ JSON حتى نجد الرقم داخل الروابط المشفّرة."""
    return unquote(unquote((text or "").replace("\\/", "/")))


def resolve_link(url: str) -> tuple[str | None, list[str]]:
    """يتتبع الرابط (مختصرًا كان أو لا) ويرجع (رقم المنتج, قائمة روابط التحويل).

    يفحص كل حلقات التحويل وليس الأخيرة فقط، لأن AliExpress قد ينهي السلسلة
    بصفحة تحقق بينما يظهر رقم المنتج في إحدى الحلقات الوسطى.
    """
    try:
        resp = session.get(url, timeout=10, allow_redirects=True, stream=True)
    except requests.RequestException as e:
        log.warning("Could not follow link %s: %s", url, e)
        return None, []

    chain = [url]
    for r in resp.history:
        chain.append(r.url)
        loc = r.headers.get("Location")
        if loc:
            chain.append(loc)
    chain.append(resp.url)

    log.info("Redirect chain: %s", " -> ".join(chain))
    decoded = [_decode(c) for c in chain]

    # 1) أنماط صارمة على كل حلقات السلسلة
    for c in decoded:
        pid = extract_product_id(c)
        if pid:
            resp.close()
            return pid, chain

    # 2) بعض الروابط المختصرة تحوّل عبر JavaScript: نقرأ محتوى الصفحة
    try:
        body = _decode(resp.text[:300_000])
    except Exception:  # noqa: BLE001
        body = ""
    resp.close()
    pid = extract_product_id(body)
    if pid:
        return pid, chain

    # 3) آخر خيار: أنماط عامة على الرابط النهائي فقط
    pid = extract_product_id(decoded[-1], loose=True)
    if not pid:
        log.warning("No product id found. Redirect chain: %s", " -> ".join(chain))
    return pid, chain


def affiliate_link(url: str) -> str | None:
    """يرجع رابط الإحالة أو None. سبب الفشل يُسجَّل في السجل فقط.

    الردّ الفارغ (بدون promotion_link) ثابت لهذا الرابط فلا نعيد المحاولة به،
    أما الأخطاء (شبكة / حد الطلبات) فنعيد المحاولة.
    """
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            links = call_api(api.get_affiliate_links, url)
            item = links[0] if links else None
            link = getattr(item, "promotion_link", None)
            if link:
                return link
            log.warning("No promotion_link for %s: %s", url, getattr(item, "__dict__", links))
            return None
        except Exception as e:  # noqa: BLE001
            log.warning("Attempt %d/%d failed (%s): %s: %s",
                        attempt, RETRY_ATTEMPTS, url, type(e).__name__, e)
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_DELAY)
    return None


def first_candidate(candidates: list[tuple[str, str]]) -> tuple[str | None, str | None]:
    """يجرّب (رابط, تسمية) بالترتيب ويرجع أول رابط إحالة ينجح مع تسميته."""
    for url, label in candidates:
        link = affiliate_link(url)
        if link:
            log.info("Affiliate link created (tracking id: %s) from: %s", TRACKING_ID, url)
            return link, label
    return None, None


def _meta(page: str, prop: str) -> str | None:
    """يقرأ قيمة وسم <meta property="..."> من HTML."""
    for pat in (
        r'<meta[^>]+property=["\']' + prop + r'["\'][^>]*content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*property=["\']' + prop + r'["\']',
    ):
        m = re.search(pat, page, re.I)
        if m:
            return html.unescape(m.group(1)).strip() or None
    return None


def scrape_product_info(product_id: str) -> tuple[str | None, str | None]:
    """احتياطي: يقرأ الاسم والصورة من وسوم og في صفحة المنتج (قد يُحجب أحيانًا)."""
    try:
        r = session.get(f"https://www.aliexpress.com/item/{product_id}.html", timeout=6)
        page = r.text[:400_000]
    except Exception as e:  # noqa: BLE001
        log.warning("Product page request failed: %s", e)
        return None, None

    title = _meta(page, "og:title")
    image = _meta(page, "og:image")
    if title and re.search(r"captcha|interception|punish", title, re.I):
        title = None
    if title:
        title = re.sub(r"\s*[-|]\s*AliExpress.*$", "", title, flags=re.I).strip() or None
    if image and image.startswith("//"):
        image = "https:" + image
    if image and not image.startswith("http"):
        image = None
    return title, image


def api_product_info(product_id: str) -> tuple[str | None, str | None]:
    """احتياطي: الاسم والصورة من الـ API (محاولة واحدة، حصته محدودة)."""
    try:
        products = call_api(
            api.get_products_details,
            [product_id], fields=["product_title", "product_main_image_url"],
        )
        if products:
            p = products[0]
            title = (getattr(p, "product_title", "") or "").strip() or None
            image = (getattr(p, "product_main_image_url", "") or "").strip() or None
            return title, image
    except Exception as e:  # noqa: BLE001
        log.warning("API product info failed: %s: %s", type(e).__name__, e)
    return None, None


# --------------------------------------------------------------------------
# أوامر تلغرام
# --------------------------------------------------------------------------
@bot.message_handler(commands=["start"])
def cmd_start(message):
    bot.send_message(
        message.chat.id,
        "مرحبًا 👋\nأرسل لي أي رسالة تحتوي على رابط منتج من AliExpress "
        "وسأعطيك رابط العملات، أو رابط الباندل إذا كان رابطك لعرض باندل.\n\n/help للمساعدة.",
    )


@bot.message_handler(commands=["help"])
def cmd_help(message):
    bot.send_message(
        message.chat.id,
        "طريقة الاستخدام:\n"
        "1. أرسل رابط منتج AliExpress (عادي أو مختصر)، ويمكن أن يكون ضمن نص.\n"
        "2. إذا كان رابط منتج عادي ← أرسل لك 🪙 رابط العملات.\n"
        "3. إذا كان رابط عرض باندل ← أرسل لك 📦 رابط الباندل.",
    )


@bot.message_handler(content_types=["text", "photo", "video", "document"])
def handle_message(message):
    text = f"{message.text or ''} {message.caption or ''}"
    urls = find_urls(text)

    ali_urls = [u for u in urls if is_aliexpress(u)]
    if not ali_urls:
        bot.send_message(message.chat.id, "❌ الرابط غير صالح: لم أجد رابط AliExpress في رسالتك.")
        return

    waiting = bot.send_message(message.chat.id, "⏳ جاري المعالجة...")

    try:
        original = ali_urls[0]
        product_id = None
        chain: list[str] = []
        # إذا كان الرابط يحمل رقم المنتج أصلًا نتجنب طلب الشبكة (أسرع)
        direct = "/item/" in original or "productIds=" in original
        if direct:
            product_id = extract_product_id(_decode(original))
        if not product_id:
            product_id, chain = resolve_link(original)
        if not product_id:
            bot.edit_message_text(
                "❌ لم أتمكن من التعرف على المنتج من هذا الرابط.",
                message.chat.id, waiting.message_id,
            )
            return

        # نوع الرابط: باندل => رابط باندل، غير ذلك => رابط العملات.
        # مهم جدًا: نبني الرابط دائمًا من رقم المنتج فقط (رابط نظيف) ثم نمرره للـ API
        # ليُنسب الرابط الناتج إلى ALIEXPRESS_TRACKING_ID الخاص بك. لا نمرر أبدًا
        # الرابط الأصلي ولا أي رابط من سلسلة التحويل، لأنه قد يحمل تتبّع شخص آخر
        # فتذهب العمولة له أو لا تُنسب لحسابك.
        bundle = is_bundle_url(original, *chain)
        if bundle:
            candidates = [(BUNDLE_URL_TEMPLATE.format(id=product_id), LABEL_BUNDLE)]
        else:
            candidates = [(t.format(id=product_id), LABEL_COIN) for t in COIN_URL_TEMPLATES]
            candidates.append((PLAIN_URL_TEMPLATE.format(id=product_id), LABEL_FALLBACK))

        # الاسم والصورة من صفحة المنتج (لا يستهلك حصة الـ API) بالتوازي مع توليد الرابط
        with ThreadPoolExecutor(max_workers=1) as pool:
            page_f = pool.submit(scrape_product_info, product_id)
            link, label = first_candidate(candidates)
            title, image = page_f.result()
        if not link:
            label = LABEL_BUNDLE if bundle else LABEL_COIN

        # إذا نقص الاسم أو الصورة نحاول الـ API مرة واحدة
        if not title or not image:
            api_title, api_image = api_product_info(product_id)
            title = title or api_title
            image = image or api_image

        parts = []
        if title:
            parts.append(f"🛍 <b>{html.escape(title)}</b>")
        parts.append(
            f"{label}\n{html.escape(link)}" if link
            else f"{label}\n❌ لم يقبل AliExpress إنشاء رابط مرتبط بحسابك لهذا المنتج"
        )
        caption = "\n\n".join(parts)

        # مع صورة: نرسلها والرابط في الوصف. إذا رفضها تلغرام نرجع لرسالة نصية.
        if image and len(caption) <= 1024:
            try:
                bot.send_photo(message.chat.id, image, caption=caption, parse_mode="HTML")
            except Exception as e:  # noqa: BLE001
                log.warning("send_photo failed, falling back to text: %s", e)
            else:
                try:
                    bot.delete_message(message.chat.id, waiting.message_id)
                except Exception:  # noqa: BLE001
                    pass
                return

        bot.edit_message_text(
            caption, message.chat.id, waiting.message_id,
            parse_mode="HTML", disable_web_page_preview=True,
        )

    except Exception:  # noqa: BLE001
        log.exception("Unexpected error")
        bot.edit_message_text(
            "❌ حدث خطأ، حاول مرة أخرى.", message.chat.id, waiting.message_id
        )


if __name__ == "__main__":
    try:
        me = bot.get_me()
        log.info("Connected to Telegram as @%s", me.username)
        log.info("Tracking ID in use: %s  (must match the ID in your AliExpress portal)", TRACKING_ID)
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"Cannot connect to Telegram - check TELEGRAM_BOT_TOKEN and internet: {e}")

    # إذا كان البوت القديم يعمل بـ Webhook فلن يصل أي تحديث للـ polling حتى يُحذف
    bot.remove_webhook()
    log.info("Bot is running. Send it a link.")
    bot.infinity_polling(skip_pending=True)
