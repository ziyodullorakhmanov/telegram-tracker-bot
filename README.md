# Daily Task & Goal Tracker Bot

## 1. Botni Telegram'da yaratish
1. Telegram'da **@BotFather** ga yozing
2. `/newbot` buyrug'ini yuboring, nom va username bering
3. Sizga **token** beriladi (masalan `123456789:AAExampleToken...`) — buni saqlab qo'ying

## 2. Lokalda sinab ko'rish (ixtiyoriy)
```bash
pip install -r requirements.txt
export BOT_TOKEN="sizning_tokeningiz"
python bot.py
```
Bot ishga tushadi va siz Telegram'da unga `/start` yozishingiz mumkin.
**Diqqat:** kompyuteringizni o'chirsangiz, bot ham to'xtaydi.

## 3. Doimiy ishlashi uchun — Railway.app orqali deploy (bepul, oson)

1. https://railway.app ga kiring, GitHub akkountingiz bilan ro'yxatdan o'ting
2. Bu papkani (`telegram_bot/`) o'zingizning GitHub repo'ingizga yuklang:
   ```bash
   git init
   git add .
   git commit -m "Telegram tracker bot"
   git remote add origin <sizning_repo_url>
   git push -u origin main
   ```
3. Railway'da **New Project → Deploy from GitHub repo** tanlang, shu repo'ni tanlang
4. **Variables** bo'limiga kirib, quyidagini qo'shing:
   - `BOT_TOKEN` = sizning tokeningiz
5. **Settings → Start Command** ga yozing:
   ```
   python bot.py
   ```
6. Deploy tugaganidan so'ng bot 24/7 ishlab turadi.

> Railway bepul tarifi oyiga cheklangan soatlarni beradi (odatda kichik shaxsiy bot uchun yetarli). Agar tugab qolsa, muqobil sifatida **Render.com** (Background Worker) yoki **PythonAnywhere** dan foydalanish mumkin — logikasi bir xil.

## 4. Botdan foydalanish

| Buyruq | Vazifasi |
|---|---|
| `/start` | Ro'yxatdan o'tish va yo'riqnoma |
| `/goal <matn>` | Bugungi maqsad/task qo'shish |
| `/goals` | Bugungi tasklar ro'yxati (tugmalar bilan bajarish) |
| `/done <raqam>` | Taskni bajarildi deb belgilash |
| `/report` | Kunni qo'lda yakunlash va baholash |
| `/stats` | Oxirgi 7 kunlik statistika |
| `/settime HH:MM` | Kechki eslatma vaqtini o'zgartirish (standart 21:00) |

Oddiy xabar yozsangiz (masalan, "loyihaning API qismini tugatdim"), bot buni kundaligingizga yozib qo'yadi va joriy kun progressini darhol ko'rsatadi.

Har kuni belgilangan vaqtda (standart 21:00) bot sizga eslatma yuborib, kunni **1–10** oralig'ida baholashingizni so'raydi. Bu baholar `/stats` da o'rtacha reyting sifatida ko'rinadi.

## 5. Ma'lumotlar qayerda saqlanadi?
Barcha tasklar, loglar va reytinglar `tracker.db` (SQLite) faylida saqlanadi — bot papkasida avtomatik yaratiladi. Zaxira nusxa olish uchun shu faylni vaqti-vaqti bilan ko'chirib qo'yish tavsiya etiladi (Railway'da disk vaqti-vaqti bilan tozalanishi mumkin — doimiy saqlash uchun Railway'ning **Volume** funksiyasidan foydalaning).

## 6. Vaqt zonasi haqida eslatma
Server (Railway) odatda UTC vaqtida ishlaydi. Agar `/settime 21:00` yozsangiz, bu server vaqti bo'yicha 21:00 bo'ladi, O'zbekiston vaqti (UTC+5) emas. Toshkent vaqti bilan 21:00 kechqurun bo'lishi uchun `/settime 16:00` deb yozing (21:00 - 5 soat = 16:00 UTC).
