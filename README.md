# ⚡ Shopee Flash Sale Sniper Bot v2

Bot auto-serbu flash sale Shopee — bisa cari sendiri produk Rp 1!

## 🚀 Quick Start

### 1. Ambil Cookie Shopee
1. Login **shopee.co.id** di Chrome
2. Install ekstensi **Cookie-Editor** (by cgagnier)
3. Klik icon Cookie-Editor → **Export** → **JSON**
4. Simpan sebagai `/root/shopee-bot/cookies.json`

### 2. Jalankan Bot

**Mode Auto-Scan** (bot cari produk murah sendiri):
```bash
cd /root/shopee-bot
python3 flash_sale.py
```

**Mode Manual** (lo kasih link produk):
```bash
python3 flash_sale.py "https://shopee.co.id/Produk-Name-i.123456.789012"
```

**Mode Manual + Timer** (target waktu flash sale):
```bash
python3 flash_sale.py "https://shopee.co.id/..." 1735683600
```

## ⚙️ Config (di flash_sale.py)

```python
AUTO_SCAN = True        # Bot cari produk murah otomatis
MAX_PRICE = 1000        # Harga maksimum Rp (default Rp 1.000)
SCAN_PAGES = 5          # Berapa halaman flash sale di-scan
QUANTITY = 1            # Jumlah item dibeli
CONCURRENT_REQUESTS = 5 # Request checkout paralel
PAYMENT_CHANNEL_ID = 8001400  # ShopeePay
```

## 💳 Payment Channels

| Channel | ID |
|---------|------|
| ShopeePay | 8001400 |
| COD | 89000 |
| Transfer Bank | 8005200 |
| BCA | 89052001 |
| Mandiri | 89052002 |
| BNI | 89052003 |
| BRI | 89052004 |

## 🎯 Tips Menang

- ⏱️ NTP sync = timing akurat ±5ms
- 🚀 5 concurrent checkout = peluang 5x lebih besar
- 🔄 Auto-retry kalau rate limited
- 📡 Direct API = jauh lebih cepat dari browser
- 🍪 Cookie harus fresh (re-export kalau expired)
