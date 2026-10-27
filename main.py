from fastapi import FastAPI, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
import base64
import requests
from PIL import Image
import io
import sqlite3
import hashlib
import json
import os
import re
from urllib.parse import quote
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

YANDEX_API_KEY = os.getenv("YANDEX_API_KEY")
FOLDER_ID = os.getenv("FOLDER_ID")
DB_NAME = "stylist.db"
AFFILIATE_PREFIX = ""

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS analyses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        image_hash TEXT UNIQUE,
        gender TEXT,
        recommendation TEXT,
        products_json TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS cache (
        image_hash TEXT PRIMARY KEY,
        recommendation TEXT,
        products_json TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()
    print("✅ База данных готова")

init_db()

def get_image_hash(image_bytes: bytes) -> str:
    return hashlib.md5(image_bytes).hexdigest()

def compress_image(image_bytes: bytes, max_size_kb: int = 800) -> bytes:
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode in ('RGBA', 'P'):
        img = img.convert('RGB')
    max_dimension = 1024
    if img.width > max_dimension or img.height > max_dimension:
        ratio = max_dimension / max(img.width, img.height)
        new_size = (int(img.width * ratio), int(img.height * ratio))
        img = img.resize(new_size, Image.Resampling.LANCZOS)
    quality = 75
    while quality > 20:
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=quality, optimize=True)
        if buffer.tell() / 1024 < max_size_kb:
            return buffer.getvalue()
        quality -= 10
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=65, optimize=True)
    return buffer.getvalue()

def search_wildberries(query: str, limit: int = 3):
    try:
        url = f"https://search.wb.ru/exactmatch/ru/common/v4/search?query={quote(query)}&resultset=catalog&limit={limit + 5}&sort=popular&spp=30"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        response = requests.get(url, headers=headers, timeout=10)
        data = response.json()
        products = []
        for item in data.get("data", {}).get("products", []):
            if len(products) >= limit:
                break
            item_id = item.get("id", 0)
            basket_num = item_id // 100000
            vol = item_id // 1000
            image_url = f"https://basket-{basket_num:02d}.wbbasket.ru/vol{vol}/part{item_id // 100}/{item_id}/images/big/1.webp"
            product_link = f"https://www.wildberries.ru/catalog/{item_id}/detail.aspx"
            if AFFILIATE_PREFIX:
                product_link = f"{AFFILIATE_PREFIX}{quote(product_link)}"
            products.append({
                "name": item.get("name", "Товар"),
                "price": item.get("salePriceU", 0) // 100,
                "old_price": item.get("priceU", 0) // 100,
                "image": image_url,
                "link": product_link,
                "marketplace": "WB"
            })
        return products
    except Exception as e:
        print(f"❌ Ошибка поиска WB: {e}")
        return []

@app.get("/")
async def serve_frontend():
    return FileResponse("index.html")

@app.post("/analyze")
async def analyze_image(file: UploadFile = File(...), gender: str = Form("женщина")):
    try:
        image_bytes = await file.read()
        image_hash = get_image_hash(image_bytes)
        
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT recommendation, products_json FROM cache WHERE image_hash = ?", (image_hash,))
        cached = cursor.fetchone()
        conn.close()
        
        if cached:
            print(f"💾 Найдено в кэше: {image_hash}")
            return {
                "status": "success",
                "recommendation": cached[0],
                "products": json.loads(cached[1]) if cached[1] else [],
                "from_cache": True
            }
        
        image_bytes = compress_image(image_bytes)
        image_base64 = base64.b64encode(image_bytes).decode("utf-8")
        
        url = "https://ai.api.cloud.yandex.net/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Api-Key {YANDEX_API_KEY}",
            "OpenAI-Project": FOLDER_ID,
        }
        
        prompt = f"""Ты — элитный AI-стилист с опытом работы в Vogue и Balenciaga. 
КРИТИЧЕСКИ ВАЖНО: На фото изображен {gender.upper()}. Подбирай СТРОГО только {gender}скую одежду.

ТВОЯ ЗАДАЧА:
Проанализируй внешность: цветотип, тип фигуры, текущий стиль.
Предложи 3 модных образа для РАЗНЫХ СИТУАЦИЙ:
Образ 1: Для работы/офиса
Образ 2: Для свидания/вечера
Образ 3: Для выходных/прогулки или спорта

ДЛЯ КАЖДОГО ОБРАЗА УКАЖИ:
• Конкретные вещи (фасон, крой)
• Цветовую палитру и материалы
• Ценовую категорию (Бюджет, Средний, Премиум)

КРИТИЧЕСКИ ВАЖНО: Для каждого образа сгенерируй ТРИ ОТДЕЛЬНЫХ коротких поисковых запроса для маркетплейсов:
ЗАПРОС_ВЕРХ: для поиска верхней вещи (например: "белая блузка оверсайз женская шелк")
ЗАПРОС_НИЗ: для поиска нижней вещи (например: "черная юбка миди плиссе женская")
ЗАПРОС_ОБУВЬ: для поиска обуви и аксессуаров (например: "бежевые лодочки женские кожа")
Каждый запрос должен быть КОРОТКИМ (3-6 слов) и содержать только ОДНУ ключевую вещь.

ВАЖНО: ВСЕ ТРИ ЗАПРОСА ДОЛЖНЫ БЫТЬ ЗАПОЛНЕНЫ ДЛЯ ВСЕХ ТРЁХ ОБРАЗОВ. НЕ ОСТАВЛЯЙ ПУСТЫМИ!

ОБЯЗАТЕЛЬНО ДОБАВЛЯЙ БЛОК "ПОЧЕМУ ЭТО МОДНО" (скрытый под спойлер):
Внутри спойлера укажи актуальный тренд и бренд-вдохновитель.

КРИТИЧЕСКИ ВАЖНО: Используй ТОЛЬКО ссылки из этого списка. Не выдумывай URL!
СПИСОК БРЕНДОВ И ССЫЛОК:
Zara: https://www.zara.com/ru/
The Row: https://www.therow.com/
Karl Lagerfeld: https://www.karl.com/ru_ru/
Benetton: https://www.benetton.com/ru/
Prada: https://www.prada.com/ru/ru.html
Kanzler: https://www.kanzler.ru/
Henderson: https://henderson.ru/
Marco Polo: https://marcopolo-brand.ru/
Nike: https://www.nike.com/ru/
Adidas: https://www.adidas.com/ru
Puma: https://ru.puma.com/
Fila: https://www.fila.ru/
Jordan: https://www.nike.com/ru/w/jordan-37eef

ФОРМАТ ОТВЕТА:
Отвечай ТОЛЬКО чистым HTML-кодом. НЕ используй markdown. Начинай сразу с <h2>.
КРИТИЧЕСКИ ВАЖНО: Для КАЖДОГО из трёх образов ты ДОЛЖЕН полностью повторить блок <details class="shop-details"> со ВСЕМИ ТРЕМЯ строками (Верх, Низ, Обувь). НИКОГДА не пропускай строки и не пиши "не требуется".

<h2>🔍 Анализ внешности</h2>
<p>Краткое описание: цветотип, тип фигуры, текущий стиль</p>

<h2>👔 Образ 1: Для работы</h2>
<p><b>Стиль:</b> [описание стиля]</p>
<ul>
<li><b>Верх:</b> [конкретная вещь]</li>
<li><b>Низ:</b> [конкретная вещь]</li>
<li><b>Обувь:</b> [конкретная вещь]</li>
<li><b>Аксессуары:</b> [конкретная вещь]</li>
</ul>
<p><b>Цвета:</b> [палитра] | <b>Материалы:</b> [материалы] | <b>Бюджет:</b> [категория]</p>
<details class="trend-details">
<summary>🔥 Почему это модно? (Тренды и бренды)</summary>
<div class="trend-content">
<p><b>Тренд 2025/26:</b> [Название тренда]</p>
<p><b>Вдохновение:</b> Эстетика бренда <a href="[ССЫЛКА_ИЗ_СПИСКА]" target="_blank" class="brand-link">[Название бренда]</a>. [Краткое пояснение]</p>
</div>
</details>
<details class="shop-details">
<summary>🛍 Собрать образ по вещам</summary>
<div class="shop-content">
<div class="shop-row">
<span class="shop-label">👕 Верх:</span>
<div class="marketplace-buttons">
<a href="https://www.wildberries.ru/catalog/0/search.aspx?search=[ЗАПРОС_ВЕРХ]" target="_blank" class="market-btn wb-btn">WB</a>
<a href="https://www.ozon.ru/search/?text=[ЗАПРОС_ВЕРХ]" target="_blank" class="market-btn ozon-btn">Ozon</a>
<a href="https://www.lamoda.ru/catalog/?q=[ЗАПРОС_ВЕРХ]" target="_blank" class="market-btn lamoda-btn">Lamoda</a>
</div>
</div>
<div class="shop-row">
<span class="shop-label">👖 Низ:</span>
<div class="marketplace-buttons">
<a href="https://www.wildberries.ru/catalog/0/search.aspx?search=[ЗАПРОС_НИЗ]" target="_blank" class="market-btn wb-btn">WB</a>
<a href="https://www.ozon.ru/search/?text=[ЗАПРОС_НИЗ]" target="_blank" class="market-btn ozon-btn">Ozon</a>
<a href="https://www.lamoda.ru/catalog/?q=[ЗАПРОС_НИЗ]" target="_blank" class="market-btn lamoda-btn">Lamoda</a>
</div>
</div>
<div class="shop-row">
<span class="shop-label">👟 Обувь и аксессуары:</span>
<div class="marketplace-buttons">
<a href="https://www.wildberries.ru/catalog/0/search.aspx?search=[ЗАПРОС_ОБУВЬ]" target="_blank" class="market-btn wb-btn">WB</a>
<a href="https://www.ozon.ru/search/?text=[ЗАПРОС_ОБУВЬ]" target="_blank" class="market-btn ozon-btn">Ozon</a>
<a href="https://www.lamoda.ru/catalog/?q=[ЗАПРОС_ОБУВЬ]" target="_blank" class="market-btn lamoda-btn">Lamoda</a>
</div>
</div>
</div>
</details>

<h2>💕 Образ 2: Для свидания</h2>
<p><b>Стиль:</b> [описание стиля]</p>
<ul>
<li><b>Верх:</b> [конкретная вещь]</li>
<li><b>Низ:</b> [конкретная вещь]</li>
<li><b>Обувь:</b> [конкретная вещь]</li>
<li><b>Аксессуары:</b> [конкретная вещь]</li>
</ul>
<p><b>Цвета:</b> [палитра] | <b>Материалы:</b> [материалы] | <b>Бюджет:</b> [категория]</p>
<details class="trend-details">
<summary>🔥 Почему это модно? (Тренды и бренды)</summary>
<div class="trend-content">
<p><b>Тренд 2025/26:</b> [Название тренда]</p>
<p><b>Вдохновение:</b> Эстетика бренда <a href="[ССЫЛКА_ИЗ_СПИСКА]" target="_blank" class="brand-link">[Название бренда]</a>. [Краткое пояснение]</p>
</div>
</details>
<details class="shop-details">
<summary>🛍 Собрать образ по вещам</summary>
<div class="shop-content">
<div class="shop-row">
<span class="shop-label">👕 Верх:</span>
<div class="marketplace-buttons">
<a href="https://www.wildberries.ru/catalog/0/search.aspx?search=[ЗАПРОС_ВЕРХ]" target="_blank" class="market-btn wb-btn">WB</a>
<a href="https://www.ozon.ru/search/?text=[ЗАПРОС_ВЕРХ]" target="_blank" class="market-btn ozon-btn">Ozon</a>
<a href="https://www.lamoda.ru/catalog/?q=[ЗАПРОС_ВЕРХ]" target="_blank" class="market-btn lamoda-btn">Lamoda</a>
</div>
</div>
<div class="shop-row">
<span class="shop-label">👖 Низ:</span>
<div class="marketplace-buttons">
<a href="https://www.wildberries.ru/catalog/0/search.aspx?search=[ЗАПРОС_НИЗ]" target="_blank" class="market-btn wb-btn">WB</a>
<a href="https://www.ozon.ru/search/?text=[ЗАПРОС_НИЗ]" target="_blank" class="market-btn ozon-btn">Ozon</a>
<a href="https://www.lamoda.ru/catalog/?q=[ЗАПРОС_НИЗ]" target="_blank" class="market-btn lamoda-btn">Lamoda</a>
</div>
</div>
<div class="shop-row">
<span class="shop-label">👟 Обувь и аксессуары:</span>
<div class="marketplace-buttons">
<a href="https://www.wildberries.ru/catalog/0/search.aspx?search=[ЗАПРОС_ОБУВЬ]" target="_blank" class="market-btn wb-btn">WB</a>
<a href="https://www.ozon.ru/search/?text=[ЗАПРОС_ОБУВЬ]" target="_blank" class="market-btn ozon-btn">Ozon</a>
<a href="https://www.lamoda.ru/catalog/?q=[ЗАПРОС_ОБУВЬ]" target="_blank" class="market-btn lamoda-btn">Lamoda</a>
</div>
</div>
</div>
</details>

<h2>🌿 Образ 3: Для выходных</h2>
<p><b>Стиль:</b> [описание стиля]</p>
<ul>
<li><b>Верх:</b> [конкретная вещь]</li>
<li><b>Низ:</b> [конкретная вещь]</li>
<li><b>Обувь:</b> [конкретная вещь]</li>
<li><b>Аксессуары:</b> [конкретная вещь]</li>
</ul>
<p><b>Цвета:</b> [палитра] | <b>Материалы:</b> [материалы] | <b>Бюджет:</b> [категория]</p>
<details class="trend-details">
<summary>🔥 Почему это модно? (Тренды и бренды)</summary>
<div class="trend-content">
<p><b>Тренд 2025/26:</b> [Название тренда]</p>
<p><b>Вдохновение:</b> Эстетика бренда <a href="[ССЫЛКА_ИЗ_СПИСКА]" target="_blank" class="brand-link">[Название бренда]</a>. [Краткое пояснение]</p>
</div>
</details>
<details class="shop-details">
<summary>🛍 Собрать образ по вещам</summary>
<div class="shop-content">
<div class="shop-row">
<span class="shop-label">👕 Верх:</span>
<div class="marketplace-buttons">
<a href="https://www.wildberries.ru/catalog/0/search.aspx?search=[ЗАПРОС_ВЕРХ]" target="_blank" class="market-btn wb-btn">WB</a>
<a href="https://www.ozon.ru/search/?text=[ЗАПРОС_ВЕРХ]" target="_blank" class="market-btn ozon-btn">Ozon</a>
<a href="https://www.lamoda.ru/catalog/?q=[ЗАПРОС_ВЕРХ]" target="_blank" class="market-btn lamoda-btn">Lamoda</a>
</div>
</div>
<div class="shop-row">
<span class="shop-label">👖 Низ:</span>
<div class="marketplace-buttons">
<a href="https://www.wildberries.ru/catalog/0/search.aspx?search=[ЗАПРОС_НИЗ]" target="_blank" class="market-btn wb-btn">WB</a>
<a href="https://www.ozon.ru/search/?text=[ЗАПРОС_НИЗ]" target="_blank" class="market-btn ozon-btn">Ozon</a>
<a href="https://www.lamoda.ru/catalog/?q=[ЗАПРОС_НИЗ]" target="_blank" class="market-btn lamoda-btn">Lamoda</a>
</div>
</div>
<div class="shop-row">
<span class="shop-label">👟 Обувь и аксессуары:</span>
<div class="marketplace-buttons">
<a href="https://www.wildberries.ru/catalog/0/search.aspx?search=[ЗАПРОС_ОБУВЬ]" target="_blank" class="market-btn wb-btn">WB</a>
<a href="https://www.ozon.ru/search/?text=[ЗАПРОС_ОБУВЬ]" target="_blank" class="market-btn ozon-btn">Ozon</a>
<a href="https://www.lamoda.ru/catalog/?q=[ЗАПРОС_ОБУВЬ]" target="_blank" class="market-btn lamoda-btn">Lamoda</a>
</div>
</div>
</div>
</details>

<h2>💡 Совет от стилиста</h2>
<p>Короткий профессиональный совет по деталям, которые сделают образ дорогим.</p>"""

        payload = {
            "model": f"gpt://{FOLDER_ID}/yandexgpt/latest",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
                    ]
                }
            ],
            "temperature": 0.7,
        }
        
        print(f"🚀 Отправляем запрос в YandexGPT (Пол: {gender})...")
        response = requests.post(url, json=payload, headers=headers, timeout=90)
        if response.status_code != 200:
            print(f"❌ Ошибка API: {response.text}")
        response.raise_for_status()
        result = response.json()
        ai_text = result["choices"][0]["message"]["content"]
        
        ai_text = ai_text.strip()
        if ai_text.startswith("```html"):
            ai_text = ai_text[7:]
        elif ai_text.startswith("```"):
            ai_text = ai_text[3:]
        if ai_text.endswith("```"):
            ai_text = ai_text[:-3]
        ai_text = ai_text.strip()
        
        queries = {
            "top": re.findall(r'ЗАПРОС_ВЕРХ[123]?\s*[:=]\s*["\']?([^"\'\n]+)', ai_text),
            "bottom": re.findall(r'ЗАПРОС_НИЗ[123]?\s*[:=]\s*["\']?([^"\'\n]+)', ai_text),
            "shoes": re.findall(r'ЗАПРОС_ОБУВЬ[123]?\s*[:=]\s*["\']?([^"\'\n]+)', ai_text)
        }
        
        all_products = []
        for q in queries.get("top", [])[:3]:
            products = search_wildberries(q.strip(), limit=2)
            all_products.extend(products)
        for q in queries.get("bottom", [])[:3]:
            products = search_wildberries(q.strip(), limit=2)
            all_products.extend(products)
        for q in queries.get("shoes", [])[:3]:
            products = search_wildberries(q.strip(), limit=2)
            all_products.extend(products)
        
        all_products = all_products[:9]
        
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        products_json = json.dumps(all_products, ensure_ascii=False)
        cursor.execute(
            "INSERT OR REPLACE INTO cache (image_hash, recommendation, products_json) VALUES (?, ?, ?)",
            (image_hash, ai_text, products_json)
        )
        cursor.execute(
            "INSERT INTO analyses (image_hash, gender, recommendation, products_json) VALUES (?, ?, ?, ?)",
            (image_hash, gender, ai_text, products_json)
        )
        conn.commit()
        conn.close()
        
        return {
            "status": "success",
            "recommendation": ai_text,
            "products": all_products,
            "from_cache": False
        }
        
    except Exception as e:
        print(f"❌ Исключение: {str(e)}")
        return JSONResponse(status_code=200, content={"status": "error", "detail": str(e)})

@app.get("/history")
async def get_history():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id, gender, created_at FROM analyses ORDER BY created_at DESC LIMIT 10")
    rows = cursor.fetchall()
    conn.close()
    return {"history": [{"id": r[0], "gender": r[1], "date": r[2]} for r in rows]}

if __name__ == "__main__":
    import uvicorn
    print("🎨 AI Стилист запускается на http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)