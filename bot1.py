import html
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, unquote, urlparse

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
    raise SystemExit(f"متغيرات بيئة ناقصة: {', '.join(missing)}")

# --------------------------------------------------------------------------
# قوالب الروابط  (غير مؤكدة - جرّبها وعدّلها إذا لزم)
# {id} = رقم المنتج.
# كل قالب يُمرَّر لاحقًا إلى get_affiliate_links ليتحول إلى رابط إحالة.
# --------------------------------------------------------------------------
COIN_URL_TEMPLATE = (
    "https://www.aliexpress.com/item/{id}.html"
    "?sourceType=620&channel=coin&afSmartRedirect=y"
)
BUNDLE_URL_TEMPLATE = (
    "https://www.aliexpress.com/ssr/300000512/BundleDeals2"
    "?disableNav=YES&pha_manifest=ssr&_immersiveMode=true&productIds={id}"
)

RETRY_ATTEMPTS = 2
RETRY_DELAY = 0.5

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
# دوال مساعدة
# --------------------------------------------------------------------------
URL_RE = re.compile(r"https?://[^\s)\]>\"'<]+")
ALI_DOMAINS = ("aliexpress.com", "aliexpress.us", "ali.ski", "a.aliexpress.com")


def find_urls(text: str) -> list[str]:
    return URL_RE.findall(text or "")


def is_aliexpress(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in ALI_DOMAINS)


def resolve_redirects(url: str) -> str:
    """يتتبع الروابط المختصرة ويرجع الرابط النهائي."""
    try:
        # stream=True: نتتبع التحويلات فقط دون تحميل محتوى الصفحة (أسرع)
        resp = session.get(url, timeout=8, allow_redirects=True, stream=True)
        final = resp.url
        resp.close()
    except requests.RequestException as e:
        log.warning("فشل حل الرابط %s: %s", url, e)
        return url

    # روابط star.aliexpress.com تحمل الوجهة الحقيقية في redirectUrl
    if "star.aliexpress.com" in final:
        params = parse_qs(urlparse(final).query)
        if "redirectUrl" in params:
            return unquote(params["redirectUrl"][0])
    return final


def extract_product_id(url: str) -> str | None:
    for pattern in (r"/item/(\d+)\.html", r"productIds=(\d+)", r"[?&]id=(\d+)", r"(\d{13,})"):
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


def affiliate_link(url: str) -> str | None:
    """يرجع رابط الإحالة أو None. سبب الفشل يُسجَّل في السجل فقط."""
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            links = api.get_affiliate_links(url)
            if links and links[0].promotion_link:
                return links[0].promotion_link
            reason = "رد فارغ من AliExpress"
        except Exception as e:  # noqa: BLE001
            reason = f"{type(e).__name__}: {e}"
        log.warning("محاولة %d/%d فشلت (%s): %s", attempt, RETRY_ATTEMPTS, url, reason)
        if attempt < RETRY_ATTEMPTS:
            time.sleep(RETRY_DELAY)
    return None


def product_info(product_id: str) -> tuple[str | None, str | None]:
    """يرجع (اسم المنتج, رابط الصورة). اختياري - أي فشل يُتجاهل."""
    try:
        products = api.get_products_details(
            [product_id], fields=["product_title", "product_main_image_url"]
        )
        if products:
            p = products[0]
            title = (getattr(p, "product_title", "") or "").strip() or None
            image = (getattr(p, "product_main_image_url", "") or "").strip() or None
            return title, image
    except Exception as e:  # noqa: BLE001
        log.info("تعذر جلب بيانات المنتج: %s", e)
    return None, None


# --------------------------------------------------------------------------
# أوامر تلغرام
# --------------------------------------------------------------------------
@bot.message_handler(commands=["start"])
def cmd_start(message):
    bot.send_message(
        message.chat.id,
        "مرحبًا 👋\nأرسل لي أي رسالة تحتوي على رابط منتج من AliExpress "
        "وسأعطيك رابط العملات ورابط الباندل.\n\n/help للمساعدة.",
    )


@bot.message_handler(commands=["help"])
def cmd_help(message):
    bot.send_message(
        message.chat.id,
        "طريقة الاستخدام:\n"
        "1. أرسل رابط منتج AliExpress (عادي أو مختصر)، ويمكن أن يكون ضمن نص.\n"
        "2. سأرسل لك:\n"
        "   🪙 رابط العملات\n"
        "   📦 رابط الباندل",
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
        resolved = resolve_redirects(ali_urls[0])
        product_id = extract_product_id(resolved)
        if not product_id:
            bot.edit_message_text(
                "❌ لم أتمكن من التعرف على المنتج من هذا الرابط.",
                message.chat.id, waiting.message_id,
            )
            return

        # الطلبات الثلاثة تعمل بالتوازي بدل أن تنتظر بعضها
        with ThreadPoolExecutor(max_workers=3) as pool:
            coin_f = pool.submit(affiliate_link, COIN_URL_TEMPLATE.format(id=product_id))
            bundle_f = pool.submit(affiliate_link, BUNDLE_URL_TEMPLATE.format(id=product_id))
            info_f = pool.submit(product_info, product_id)
            coin_link = coin_f.result()
            bundle_link = bundle_f.result()
            title, image = info_f.result()

        parts = []
        if title:
            parts.append(f"🛍 <b>{html.escape(title)}</b>")
        parts.append(
            f"🪙 <b>رابط العملات:</b>\n{html.escape(coin_link)}" if coin_link
            else "🪙 <b>رابط العملات:</b>\n❌ تعذر إنشاء الرابط"
        )
        parts.append(
            f"📦 <b>رابط الباندل:</b>\n{html.escape(bundle_link)}" if bundle_link
            else "📦 <b>رابط الباندل:</b>\n❌ تعذر إنشاء الرابط"
        )

        caption = "\n\n".join(parts)

        # مع صورة: نرسلها والرابطان في الوصف. إذا رفضها تلغرام نرجع لرسالة نصية.
        if image and len(caption) <= 1024:
            try:
                bot.send_photo(message.chat.id, image, caption=caption, parse_mode="HTML")
            except Exception as e:  # noqa: BLE001
                log.warning("فشل إرسال الصورة، سيُرسل نص فقط: %s", e)
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

    except Exception as e:  # noqa: BLE001
        log.exception("خطأ غير متوقع")
        bot.edit_message_text(
            "❌ حدث خطأ، حاول مرة أخرى.", message.chat.id, waiting.message_id
        )


if __name__ == "__main__":
    log.info("البوت يعمل...")
    bot.infinity_polling(skip_pending=True)
