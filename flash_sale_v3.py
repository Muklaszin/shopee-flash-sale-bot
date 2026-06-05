"""
Shopee Flash Sale Sniper Bot v3 (Termux Edition)
- No external dependencies (uses only Python stdlib)
- Auto-scan flash sale page for cheap items
- Direct API checkout (no browser)
- Simple NTP time sync via UDP socket
"""

import json, time, hashlib, sys, os, re, struct, socket
from urllib.request import Request, urlopen
from urllib.parse import urlparse, parse_qs
from urllib.error import URLError, HTTPError
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ═══════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════

COOKIE_FILE = "cookies.json"
PRODUCT_URL = ""
TARGET_TIMESTAMP = 0
QUANTITY = 1
SUBTRACT_SECONDS = 0.5
CONCURRENT_REQUESTS = 5
REQUEST_DELAY = 0.02
MAX_RETRIES = 10
PAYMENT_CHANNEL_ID = 8001400
ADDRESS_ID = 0

AUTO_SCAN = True
MAX_PRICE = 1000
SCAN_PAGES = 5

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
    """Simple NTP query using raw UDP socket — no ntplib needed."""
    try:
        NTP_DELTA = 2208988800  # seconds between 1900 and 1970
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client.settimeout(5)
        
        # Build NTP request packet
        msg = b'\x1b' + 47 * b'\0'
        client.sendto(msg, ("pool.ntp.org", 123))
        data, _ = client.recvfrom(1024)
        client.close()
        
        # Extract timestamp from response (bytes 40-43 = seconds, 44-47 = fraction)
        seconds = struct.unpack('!I', data[40:44])[0]
        fraction = struct.unpack('!I', data[44:48])[0]
        ntp_time = seconds - NTP_DELTA + fraction / 2**32
        
        return ntp_time - time.time()
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
    return f"Rp {price_raw / 100000:,.0f}"

# ═══════════════════════════════════════════════════════════
# HTTP REQUEST (stdlib only)
# ═══════════════════════════════════════════════════════════

def http_request(method: str, url: str, headers: dict = None, body: bytes = None) -> dict:
    """Make HTTP request using only stdlib urllib."""
    req = Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        if e.code == 429:
            return {"error": -2, "error_msg": "Rate limited"}
        try:
            return json.loads(e.read().decode())
        except:
            return {"error": e.code, "error_msg": str(e)}
    except URLError as e:
        return {"error": -1, "error_msg": str(e.reason)}
    except Exception as e:
        return {"error": -1, "error_msg": str(e)}

# ═══════════════════════════════════════════════════════════
# SHOPEE API CLIENT (sync, stdlib)
# ═══════════════════════════════════════════════════════════

class ShopeeClient:
    def __init__(self, cookies: dict):
        self.cookies = cookies
        self.csrf_token = get_csrf_token(cookies)
        self.cookie_str = cookie_string(cookies)
        self.address_id = ADDRESS_ID
        self.account_info = None
        self.headers = {
            **HEADERS_BASE,
            "Cookie": self.cookie_str,
            "X-Csrftoken": self.csrf_token,
        }
    
    def _request(self, method: str, url: str, body_dict: dict = None) -> dict:
        body_str = json.dumps(body_dict, separators=(",", ":")) if body_dict else url
        headers = {**self.headers}
        headers["If-None-Match-"] = generate_if_none_match(body_str)
        
        body_bytes = body_str.encode() if body_dict else None
        if body_dict:
            headers["Content-Length"] = str(len(body_bytes))
        
        for attempt in range(3):
            result = http_request(method, url, headers=headers, body=body_bytes)
            if result.get("error") == -2:  # rate limited
                time.sleep(0.5 * (attempt + 1))
                continue
            return result
        return result
    
    def get_account_info(self) -> dict:
        data = self._request("GET", API["account_info"] + "?skip_address=1")
        if data.get("error") and data.get("error") != 0:
            raise Exception(f"Auth failed: {data}")
        self.account_info = data.get("data", {})
        return self.account_info
    
    def get_addresses(self) -> list:
        data = self._request("GET", API["addresses"])
        print(f"  📡 Address API response: {json.dumps(data)[:500]}")
        
        # Try multiple response formats
        addresses = (
            data.get("data", {}).get("addresses", [])
            or data.get("data", {}).get("address_list", [])
            or data.get("addresses", [])
            or (data.get("data", []) if isinstance(data.get("data"), list) else [])
        )
        
        if addresses and not self.address_id:
            for addr in addresses:
                if addr.get("is_default") or addr.get("is_default_address"):
                    self.address_id = addr.get("addressid") or addr.get("address_id") or addr.get("id")
                    break
            if not self.address_id and addresses:
                first = addresses[0]
                self.address_id = first.get("addressid") or first.get("address_id") or first.get("id")
        
        # If still no address, prompt user
        if not self.address_id:
            print("  ⚠️  Could not auto-detect address.")
            print("  📍 Go to Shopee App → My Account → Addresses → tap your address")
            print("  📍 The address ID is in the URL or you can check order history")
            try:
                addr_input = input("  👉 Enter your Address ID (number): ").strip()
                if addr_input.isdigit():
                    self.address_id = int(addr_input)
            except:
                pass
        
        return addresses
    
    def get_item_info(self, item_id: int, shop_id: int) -> dict:
        url = f"{API['item_info']}?itemid={item_id}&shopid={shop_id}"
        return self._request("GET", url)
    
    def get_flash_sale_sessions(self) -> list:
        all_sessions = []
        for page in range(SCAN_PAGES):
            offset = page * 20
            url = f"{API['flash_sessions']}?limit=20&offset={offset}&need_items=1&with_dp_items=1"
            data = self._request("GET", url)
            print(f"  📡 Flash sale API response (page {page}): {json.dumps(data)[:500]}")
            sessions = data.get("data", {}).get("sessions", []) or data.get("data", {}).get("flash_sale_sessions", []) or data.get("data", [])
            if not sessions:
                break
            if isinstance(sessions, list):
                all_sessions.extend(sessions)
            time.sleep(0.3)
        return all_sessions
    
    def get_flash_sale_items(self, session_id: int, item_ids: list) -> list:
        ids_str = ",".join(str(i) for i in item_ids[:50])
        url = f"{API['flash_sale']}?session_id={session_id}&item_ids={ids_str}&need_detail=1"
        data = self._request("GET", url)
        return data.get("data", {}).get("items", [])
    
    def add_to_cart(self, item_id: int, shop_id: int, model_id: int, qty: int = 1) -> dict:
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
        return self._request("POST", API["add_cart"], body_dict=body)
    
    def get_checkout(self, item_id: int, shop_id: int, model_id: int, qty: int = 1) -> dict:
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
        return self._request("POST", API["checkout_get"], body_dict=body)
    
    def place_order(self, checkout_data: dict) -> dict:
        return self._request("POST", API["place_order"], body_dict=checkout_data)

# ═══════════════════════════════════════════════════════════
# AUTO-SCAN: Find cheap flash sale items
# ═══════════════════════════════════════════════════════════

def scan_flash_sale(client: ShopeeClient) -> list:
    print(f"\n🔍 Scanning flash sale for items under Rp {MAX_PRICE:,}...")
    
    sessions = client.get_flash_sale_sessions()
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
        
        wib = timezone(timedelta(hours=7))
        print(f"\n  Session: {session_name} ({len(items)} items)")
        if start_time:
            print(f"  Time: {datetime.fromtimestamp(start_time, tz=wib).strftime('%H:%M')} - {datetime.fromtimestamp(end_time, tz=wib).strftime('%H:%M')} WIB")
        
        for item in items:
            item_id = item.get("itemid") or item.get("item_id")
            shop_id = item.get("shopid") or item.get("shop_id")
            name = item.get("name", "Unknown")
            
            price = item.get("flash_sale_price") or item.get("price") or item.get("price_max", 0)
            if isinstance(price, str):
                price = int(price)
            
            stock = item.get("stock") or item.get("flash_sale_stock", 0)
            sold = item.get("sold") or item.get("flash_sale_sold", 0)
            
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
    
    cheap_items.sort(key=lambda x: x["price_idr"])
    return cheap_items

# ═══════════════════════════════════════════════════════════
# CHECKOUT ENGINE
# ═══════════════════════════════════════════════════════════

def single_checkout(client: ShopeeClient, item_id: int, shop_id: int, model_id: int, attempt: int) -> dict:
    try:
        cart = client.add_to_cart(item_id, shop_id, model_id, QUANTITY)
        if cart.get("error") and cart.get("error") != 0:
            return {"success": False, "error": f"cart: {cart.get('error')} - {cart.get('error_msg','')}", "attempt": attempt}
        
        checkout = client.get_checkout(item_id, shop_id, model_id, QUANTITY)
        if checkout.get("error") and checkout.get("error") != 0:
            return {"success": False, "error": f"checkout: {checkout.get('error')}", "attempt": attempt}
        
        order = client.place_order(checkout)
        if order.get("error") and order.get("error") != 0:
            err = order.get("error")
            if err in [2, 9, 110]:
                return {"success": False, "error": f"FATAL: {err}", "attempt": attempt, "fatal": True}
            return {"success": False, "error": f"order: {err}", "attempt": attempt}
        
        return {"success": True, "data": order, "attempt": attempt}
    except Exception as e:
        return {"success": False, "error": str(e), "attempt": attempt}

def snipe_item(client: ShopeeClient, item: dict, ntp_offset: float):
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
            
            while wait > 2:
                time.sleep(min(wait - 1, 2))
                now = get_accurate_timestamp(ntp_offset)
                wait = start_time - SUBTRACT_SECONDS - now
            
            # Precision wait
            while get_accurate_timestamp(ntp_offset) < start_time - SUBTRACT_SECONDS:
                time.sleep(0.001)
    
    print(f"🚀 Firing {CONCURRENT_REQUESTS} checkout requests...")
    
    start = time.time()
    results = []
    
    with ThreadPoolExecutor(max_workers=CONCURRENT_REQUESTS) as executor:
        futures = []
        for i in range(CONCURRENT_REQUESTS):
            delay = i * REQUEST_DELAY
            if delay > 0:
                time.sleep(delay)
            futures.append(executor.submit(single_checkout, client, item_id, shop_id, model_id, i + 1))
        
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as e:
                results.append({"success": False, "error": str(e)})
    
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

def run():
    print("=" * 50)
    print("⚡ SHOPEE FLASH SALE SNIPER BOT v3 (Termux)")
    print("=" * 50)
    
    if not os.path.exists(COOKIE_FILE):
        print(f"❌ Cookie file not found: {COOKIE_FILE}")
        return
    
    cookies = load_cookies(COOKIE_FILE)
    print(f"✅ Loaded {len(cookies)} cookies")
    
    client = ShopeeClient(cookies)
    
    try:
        info = client.get_account_info()
        username = info.get("username", "unknown")
        print(f"👤 Logged in as: {username}")
    except Exception as e:
        print(f"❌ Auth failed: {e}")
        return
    
    client.get_addresses()
    if not client.address_id:
        print("❌ No shipping address! Add one in Shopee app.")
        return
    print(f"📍 Address ID: {client.address_id}")
    
    print("\n🕐 Syncing NTP...")
    ntp_offset = get_ntp_offset()
    print(f"⏱️  Offset: {ntp_offset*1000:.1f}ms")
    
    items_to_snipe = []
    
    if PRODUCT_URL:
        item_id, shop_id = parse_shopee_url(PRODUCT_URL)
        item_info = client.get_item_info(item_id, shop_id)
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
        items_to_snipe = scan_flash_sale(client)
        
        if not items_to_snipe:
            print("\n😞 No cheap items found in flash sale.")
            print("   Try lowering MAX_PRICE or check back later.")
            return
        
        print(f"\n{'='*50}")
        print(f"🎯 Found {len(items_to_snipe)} items under Rp {MAX_PRICE:,}!")
        for i, item in enumerate(items_to_snipe, 1):
            wib = timezone(timedelta(hours=7))
            start = datetime.fromtimestamp(item['start_time'], tz=wib).strftime('%H:%M') if item.get('start_time') else '?'
            print(f"   {i}. Rp {item['price_idr']:,.0f} | {item['name'][:40]} | Starts: {start} WIB")
    else:
        print("❌ No product URL and AUTO_SCAN is disabled!")
        return
    
    success_count = 0
    for item in items_to_snipe:
        result = snipe_item(client, item, ntp_offset)
        if result:
            success_count += 1
    
    print(f"\n{'='*50}")
    print(f"📊 Results: {success_count}/{len(items_to_snipe)} items purchased!")
    print(f"{'='*50}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        PRODUCT_URL = sys.argv[1]
    if len(sys.argv) > 2:
        TARGET_TIMESTAMP = int(sys.argv[2])
    if len(sys.argv) > 3:
        COOKIE_FILE = sys.argv[3]
    
    run()
