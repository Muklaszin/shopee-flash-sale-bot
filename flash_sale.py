"""
Shopee Flash Sale Sniper Bot v2
- Auto-scan flash sale page for cheap items (Rp 1, Rp 100, etc.)
- Direct API checkout (no browser) = maximum speed
- NTP time sync = precise timing
- Concurrent checkout requests
"""

import json, time, hashlib, asyncio, aiohttp, ntplib, sys, os, re
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone, timedelta

# ═══════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════

COOKIE_FILE = "cookies.json"
PRODUCT_URL = ""                    # Kosongkan jika mau auto-scan
TARGET_TIMESTAMP = 0                # 0 = langsung beli
QUANTITY = 1
SUBTRACT_SECONDS = 0.5
CONCURRENT_REQUESTS = 5
REQUEST_DELAY = 0.02
MAX_RETRIES = 10
PAYMENT_CHANNEL_ID = 8001400        # ShopeePay
ADDRESS_ID = 0

# Auto-scan config
AUTO_SCAN = True                    # True = bot cari produk murah sendiri
MAX_PRICE = 1000                    # Harga maksimum (Rp) untuk auto-scan
SCAN_PAGES = 5                      # Berapa halaman flash sale di-scan

# ═══════════════════════════════════════════════════════════
# SHOPEE API ENDPOINTS
# ═══════════════════════════════════════════════════════════

BASE = "https://shopee.co.id"
API = {
    "item_info":     f"{BASE}/api/v2/item/get",
    "flash_sale":    f"{BASE}/api/v4/flash_sale/flash_sale_batch_get_items",
    "flash_sessions":f"{BASE}/api/v4/flash_sale/get_all_sessions",
    "account_info":  f"{BASE}/api/v2/user/account_info",
    "addresses":     f"{BASE}/api/v1/addresses",
    "add_cart":      f"{BASE}/api/v4/cart/add_to_cart",
    "checkout_get":  f"{BASE}/api/v4/checkout/get_quick",
    "place_order":   f"{BASE}/api/v4/checkout/place_order",
}

HEADERS_BASE = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Referer": "https://shopee.co.id/",
    "Origin": "https://shopee.co.id",
    "X-Requested-With": "XMLHttpRequest",
    "X-API-Source": "pc",
    "X-Shopee-Language": "id",
    "af-ac-enc-dat": "null",
}

# ═══════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════

def generate_if_none_match(body_str: str) -> str:
    h1 = hashlib.md5(body_str.encode()).hexdigest()
    inner = hashlib.md5(("55b03" + h1 + "55b03").encode()).hexdigest()
    return f"55b03-{inner}"

def get_ntp_offset() -> float:
    try:
        c = ntplib.NTPClient()
        resp = c.request("pool.ntp.org", version=3, timeout=5)
        return resp.offset
    except Exception as e:
        print(f"⚠️  NTP sync failed ({e}), using local time")
        return 0.0

def get_accurate_timestamp(offset: float) -> float:
    return time.time() + offset

def load_cookies(path: str) -> dict:
    with open(path, "r") as f:
        raw = json.load(f)
    if isinstance(raw, list):
        return {c["name"]: c["value"] for c in raw}
    return raw

def cookie_string(cookies: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())

def get_csrf_token(cookies: dict) -> str:
    return cookies.get("csrftoken", "")

def parse_shopee_url(url: str) -> tuple:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    if "itemid" in params and "shopid" in params:
        return int(params["itemid"][0]), int(params["shopid"][0])
    match = re.search(r"i\.(\d+)\.(\d+)", url)
    if match:
        return int(match.group(2)), int(match.group(1))
    raise ValueError(f"Could not parse Shopee URL: {url}")

def format_price(price_raw: int) -> str:
    """Format Shopee price (in 1/100000 IDR) to readable string."""
    return f"Rp {price_raw / 100000:,.0f}"

# ═══════════════════════════════════════════════════════════
# SHOPEE API CLIENT
# ═══════════════════════════════════════════════════════════

class ShopeeClient:
    def __init__(self, cookies: dict):
        self.cookies = cookies
        self.csrf_token = get_csrf_token(cookies)
        self.cookie_str = cookie_string(cookies)
        self.session = None
        self.address_id = ADDRESS_ID
        self.account_info = None
    
    async def _init_session(self):
        if not self.session:
            timeout = aiohttp.ClientTimeout(total=10)
            self.session = aiohttp.ClientSession(
                headers={
                    **HEADERS_BASE,
                    "Cookie": self.cookie_str,
                    "X-Csrftoken": self.csrf_token,
                },
                timeout=timeout
            )
    
    async def _request(self, method: str, url: str, **kwargs) -> dict:
        await self._init_session()
        body = kwargs.get("json", {})
        body_str = json.dumps(body, separators=(",", ":")) if body else url
        headers = kwargs.pop("headers", {})
        headers["If-None-Match-"] = generate_if_none_match(body_str)
        
        for attempt in range(3):
            try:
                async with self.session.request(method, url, headers=headers, **kwargs) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                    data = await resp.json()
                    return data
            except Exception as e:
                if attempt == 2:
                    return {"error": -1, "error_msg": str(e)}
                await asyncio.sleep(0.3)
    
    async def get_account_info(self) -> dict:
        data = await self._request("GET", API["account_info"] + "?skip_address=1")
        if data.get("error"):
            raise Exception(f"Auth failed: {data}")
        self.account_info = data.get("data", {})
        return self.account_info
    
    async def get_addresses(self) -> list:
        data = await self._request("GET", API["addresses"])
        addresses = data.get("data", {}).get("addresses", [])
        if addresses and not self.address_id:
            for addr in addresses:
                if addr.get("is_default"):
                    self.address_id = addr["addressid"]
                    break
            if not self.address_id and addresses:
                self.address_id = addresses[0]["addressid"]
        return addresses
    
    async def get_item_info(self, item_id: int, shop_id: int) -> dict:
        url = f"{API['item_info']}?itemid={item_id}&shopid={shop_id}"
        return await self._request("GET", url)
    
    async def get_flash_sale_sessions(self) -> list:
        """Get all active flash sale sessions."""
        all_sessions = []
        for page in range(SCAN_PAGES):
            offset = page * 20
            url = f"{API['flash_sessions']}?limit=20&offset={offset}&need_items=1&with_dp_items=1"
            data = await self._request("GET", url)
            sessions = data.get("data", {}).get("sessions", [])
            if not sessions:
                break
            all_sessions.extend(sessions)
            await asyncio.sleep(0.3)
        return all_sessions
    
    async def get_flash_sale_items(self, session_id: int, item_ids: list) -> list:
        """Get detailed flash sale items for a session."""
        ids_str = ",".join(str(i) for i in item_ids[:50])
        url = f"{API['flash_sale']}?session_id={session_id}&item_ids={ids_str}&need_detail=1"
        data = await self._request("GET", url)
        return data.get("data", {}).get("items", [])
    
    async def add_to_cart(self, item_id: int, shop_id: int, model_id: int, qty: int = 1) -> dict:
        body = {
            "checkout": True,
            "client_source": 1,
            "donot_add_quantity": False,
            "itemid": item_id,
            "modelid": model_id,
            "quantity": qty,
            "shopid": shop_id,
            "source": "flash_sale",
            "update_checkout_only": False,
        }
        return await self._request("POST", API["add_cart"], json=body)
    
    async def get_checkout(self, item_id: int, shop_id: int, model_id: int, qty: int = 1) -> dict:
        body = {
            "cart_type": 1,
            "client_id": 8,
            "timestamp": int(time.time()),
            "shoporders": [{
                "shop": {"shopid": shop_id},
                "items": [{"itemid": item_id, "modelid": model_id, "quantity": qty}],
            }],
            "promotion_data": {"auto_apply_shop_voucher": False, "free_shipping_voucher_info": ""},
            "selected_payment_channel_data": {"channel_id": PAYMENT_CHANNEL_ID, "version": 2},
            "shipping_orders": [{
                "buyer_address_data": {"addressid": self.address_id},
                "shipping_id": 1,
                "shoporder_indexes": [0],
            }],
            "dropshipping_info": {"enabled": False, "name": "", "phone_number": ""},
            "device_info": {"buyer_payment_info": {}, "device_fingerprint": "", "device_id": "", "tongdun_blackbox": ""},
        }
        return await self._request("POST", API["checkout_get"], json=body)
    
    async def place_order(self, checkout_data: dict) -> dict:
        return await self._request("POST", API["place_order"], json=checkout_data)
    
    async def close(self):
        if self.session:
            await self.session.close()

# ═══════════════════════════════════════════════════════════
# AUTO-SCAN: Find cheap flash sale items
# ═══════════════════════════════════════════════════════════

async def scan_flash_sale(client: ShopeeClient) -> list:
    """Scan flash sale page for items under MAX_PRICE."""
    print(f"\n🔍 Scanning flash sale for items under Rp {MAX_PRICE:,}...")
    
    sessions = await client.get_flash_sale_sessions()
    if not sessions:
        print("❌ No flash sale sessions found (need login cookies)")
        return []
    
    print(f"📦 Found {len(sessions)} flash sale sessions")
    
    cheap_items = []
    for session in sessions:
        session_id = session.get("session_id") or session.get("id")
        session_name = session.get("name", "Unknown")
        start_time = session.get("start_time", 0)
        end_time = session.get("end_time", 0)
        
        items = session.get("items", [])
        if not items:
            continue
        
        print(f"\n  Session: {session_name} ({len(items)} items)")
        print(f"  Time: {datetime.fromtimestamp(start_time, tz=timezone(timedelta(hours=7))).strftime('%H:%M')} - {datetime.fromtimestamp(end_time, tz=timezone(timedelta(hours=7))).strftime('%H:%M')} WIB")
        
        for item in items:
            item_id = item.get("itemid") or item.get("item_id")
            shop_id = item.get("shopid") or item.get("shop_id")
            name = item.get("name", "Unknown")
            
            # Price can be in different fields
            price = item.get("flash_sale_price") or item.get("price") or item.get("price_max", 0)
            if isinstance(price, str):
                price = int(price)
            
            stock = item.get("stock") or item.get("flash_sale_stock", 0)
            sold = item.get("sold") or item.get("flash_sale_sold", 0)
            
            # Check if price is under our threshold
            price_idr = price / 100000 if price > 100000 else price
            
            if price_idr <= MAX_PRICE and stock > sold:
                model_id = item.get("modelid") or item.get("model_id", 0)
                cheap_items.append({
                    "item_id": item_id,
                    "shop_id": shop_id,
                    "model_id": model_id,
                    "name": name,
                    "price": price,
                    "price_idr": price_idr,
                    "stock": stock,
                    "sold": sold,
                    "session_id": session_id,
                    "start_time": start_time,
                    "end_time": end_time,
                })
                print(f"    💰 Rp {price_idr:,.0f} | {name[:50]} | Stock: {stock - sold} tersisa")
    
    # Sort by price
    cheap_items.sort(key=lambda x: x["price_idr"])
    return cheap_items

# ═══════════════════════════════════════════════════════════
# CHECKOUT ENGINE
# ═══════════════════════════════════════════════════════════

async def single_checkout(client: ShopeeClient, item_id: int, shop_id: int, model_id: int, attempt: int) -> dict:
    try:
        cart = await client.add_to_cart(item_id, shop_id, model_id, QUANTITY)
        if cart.get("error"):
            return {"success": False, "error": f"cart: {cart.get('error')} - {cart.get('error_msg','')}", "attempt": attempt}
        
        checkout = await client.get_checkout(item_id, shop_id, model_id, QUANTITY)
        if checkout.get("error"):
            return {"success": False, "error": f"checkout: {checkout.get('error')}", "attempt": attempt}
        
        order = await client.place_order(checkout)
        if order.get("error"):
            err = order.get("error")
            if err in [2, 9, 110]:
                return {"success": False, "error": f"FATAL: {err}", "attempt": attempt, "fatal": True}
            return {"success": False, "error": f"order: {err}", "attempt": attempt}
        
        return {"success": True, "data": order, "attempt": attempt}
    except Exception as e:
        return {"success": False, "error": str(e), "attempt": attempt}

async def delayed_checkout(client, item_id, shop_id, model_id, delay, attempt):
    if delay > 0:
        await asyncio.sleep(delay)
    return await single_checkout(client, item_id, shop_id, model_id, attempt)

async def snipe_item(client: ShopeeClient, item: dict, ntp_offset: float):
    """Snipe a single flash sale item."""
    item_id = item["item_id"]
    shop_id = item["shop_id"]
    model_id = item["model_id"]
    name = item["name"][:60]
    price = item["price_idr"]
    start_time = item.get("start_time", 0)
    
    print(f"\n{'='*50}")
    print(f"🎯 Target: {name}")
    print(f"💰 Harga: Rp {price:,.0f}")
    print(f"📦 Item: {item_id} | Shop: {shop_id} | Model: {model_id}")
    
    # Wait for flash sale start time
    if start_time > 0:
        now = get_accurate_timestamp(ntp_offset)
        wait = start_time - SUBTRACT_SECONDS - now
        if wait > 0:
            wib = timezone(timedelta(hours=7))
            start_str = datetime.fromtimestamp(start_time, tz=wib).strftime('%H:%M:%S')
            print(f"⏳ Waiting until {start_str} WIB ({wait:.0f}s)...")
            
            # Sleep in chunks
            while wait > 2:
                await asyncio.sleep(min(wait - 1, 2))
                now = get_accurate_timestamp(ntp_offset)
                wait = start_time - SUBTRACT_SECONDS - now
            
            # Precision wait
            while get_accurate_timestamp(ntp_offset) < start_time - SUBTRACT_SECONDS:
                await asyncio.sleep(0.001)
    
    print(f"🚀 Firing {CONCURRENT_REQUESTS} checkout requests...")
    
    start = time.time()
    tasks = [
        asyncio.create_task(delayed_checkout(client, item_id, shop_id, model_id, i * REQUEST_DELAY, i + 1))
        for i in range(CONCURRENT_REQUESTS)
    ]
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    elapsed = time.time() - start
    
    for r in results:
        if isinstance(r, dict) and r.get("success"):
            print(f"   ✅ SUKSES! ({elapsed:.2f}s)")
            return True
    
    for r in results:
        if isinstance(r, dict):
            print(f"   ❌ {r.get('error', 'unknown')}")
    
    return False

# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

async def run():
    print("=" * 50)
    print("⚡ SHOPEE FLASH SALE SNIPER BOT v2")
    print("=" * 50)
    
    # Load cookies
    if not os.path.exists(COOKIE_FILE):
        print(f"❌ Cookie file not found: {COOKIE_FILE}")
        print("   1. Login shopee.co.id di Chrome")
        print("   2. Install Cookie-Editor extension")
        print("   3. Export → JSON → simpan sebagai cookies.json")
        return
    
    cookies = load_cookies(COOKIE_FILE)
    print(f"✅ Loaded {len(cookies)} cookies")
    
    client = ShopeeClient(cookies)
    
    # Verify auth
    try:
        info = await client.get_account_info()
        username = info.get("username", "unknown")
        print(f"👤 Logged in as: {username}")
    except Exception as e:
        print(f"❌ Auth failed: {e}")
        await client.close()
        return
    
    # Get addresses
    await client.get_addresses()
    if not client.address_id:
        print("❌ No shipping address! Add one in Shopee app.")
        await client.close()
        return
    print(f"📍 Address ID: {client.address_id}")
    
    # NTP sync
    print("\n🕐 Syncing NTP...")
    ntp_offset = get_ntp_offset()
    print(f"⏱️  Offset: {ntp_offset*1000:.1f}ms")
    
    items_to_snipe = []
    
    if PRODUCT_URL:
        # Manual mode: user provided a specific product URL
        item_id, shop_id = parse_shopee_url(PRODUCT_URL)
        item_info = await client.get_item_info(item_id, shop_id)
        models = item_info.get("item", {}).get("models", [])
        model_id = models[0]["modelid"] if models else 0
        price = models[0].get("price", 0) if models else 0
        name = item_info.get("item", {}).get("name", "Unknown")
        
        items_to_snipe.append({
            "item_id": item_id,
            "shop_id": shop_id,
            "model_id": model_id,
            "name": name,
            "price": price,
            "price_idr": price / 100000 if price > 100000 else price,
            "start_time": TARGET_TIMESTAMP if TARGET_TIMESTAMP > 0 else 0,
        })
        print(f"\n🎯 Manual target: {name}")
    
    elif AUTO_SCAN:
        # Auto mode: scan flash sale for cheap items
        items_to_snipe = await scan_flash_sale(client)
        
        if not items_to_snipe:
            print("\n😞 No cheap items found in flash sale.")
            print("   Try lowering MAX_PRICE or check back later.")
            await client.close()
            return
        
        print(f"\n{'='*50}")
        print(f"🎯 Found {len(items_to_snipe)} items under Rp {MAX_PRICE:,}!")
        for i, item in enumerate(items_to_snipe, 1):
            wib = timezone(timedelta(hours=7))
            start = datetime.fromtimestamp(item['start_time'], tz=wib).strftime('%H:%M') if item.get('start_time') else '?'
            print(f"   {i}. Rp {item['price_idr']:,.0f} | {item['name'][:40]} | Starts: {start} WIB")
    else:
        print("❌ No product URL and AUTO_SCAN is disabled!")
        await client.close()
        return
    
    # Snipe all items
    success_count = 0
    for item in items_to_snipe:
        result = await snipe_item(client, item, ntp_offset)
        if result:
            success_count += 1
    
    # Summary
    print(f"\n{'='*50}")
    print(f"📊 Results: {success_count}/{len(items_to_snipe)} items purchased!")
    print(f"{'='*50}")
    
    await client.close()

if __name__ == "__main__":
    if len(sys.argv) > 1:
        PRODUCT_URL = sys.argv[1]
    if len(sys.argv) > 2:
        TARGET_TIMESTAMP = int(sys.argv[2])
    if len(sys.argv) > 3:
        COOKIE_FILE = sys.argv[3]
    
    asyncio.run(run())
