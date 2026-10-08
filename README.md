# adhd_products: e-Kampüs asistanı

İstanbul Ticaret Üniversitesi e-kampüsünü izleyen bir Telegram botu. Yeni ödev, duyuru, not, ders materyali, canlı ders ve takvim etkinliklerini bildirir; teslimlerden önce hatırlatır; her sabah bir özet atar. LLM bağlanırsa (DeepSeek ya da Grok) normal cümleyle soru sormak da mümkün olur. e-Kampüs'e hiçbir şey yazmaz.

## Neyi nasıl izler

| Ne | Nereden | Bildirim |
|---|---|---|
| Ödev | Ders sayfası (varlık, teslim durumu) + takvim JSON'u (tarih, açıklama) | yeni · tarih değişti/eklendi · içerik güncellendi · 24 sa ve 3 sa kala (teslim edilmediyse) |
| Not | Ders sayfasındaki "Sonuç" rozeti | girildi · değişti |
| Duyuru | Duyurular sayfası | yeni · güncellendi |
| Ders materyali | Ders sayfasındaki içerik listesi | yeni (📥 ile dosyayı Telegram'a ister) |
| Canlı ders, sınav, etkinlik | Takvim JSON'u | yeni · saat değişti · canlı dersten 15 dk önce |

Algılama kuralları (`ekampus/detect.py`):
- İlk tarama sessizdir; sadece bir "İzleme başladı" özeti gelir.
- Kaybolan bir kayıt hiçbir zaman "silindi" bildirimi üretmez.
- Hata veren ya da şüpheli görünen turlarda hiçbir şey eksik sayılmaz.
- Bildirimler outbox üzerinden gider: hiçbiri kaybolmaz, hiçbiri iki kez gelmez.

Sistem sağlığı da izlenir:
- **Siteye girilemezse** (varsayılan olarak 2 kontrol üst üste) uyarı gelir. Uyarı hatanın türünü söyler: site yanıt vermiyor mu, sunucu hata mı veriyor, bakım sayfası mı var, internet mi yok. Son başarılı kontrolün zamanı da yazar.
- Site düzelince ne kadar süre kapalı kaldığı bildirilir.
- Gece yaşanıp sabaha kadar düzelen kesintiler hiç rahatsız etmez.
- Giriş reddedilirse ya da site yapısı değişirse de uyarı gelir.

## Kurulum (Windows)

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m playwright install chromium   # daha önce kurulmadıysa
copy .env.example .env                                # sonra doldur
.venv\Scripts\python -m ekampus doctor
```

Telegram kurulumu:
1. **@BotFather** → `/newbot` ile bir bot aç ve token'ı `.env` içinde `TELEGRAM_BOT_TOKEN` alanına yaz.
2. `.venv\Scripts\python -m ekampus bot` ile botu başlat ve bota `/start` yaz. Bot sana chat id'ni söyler.
3. Chat id'yi `TELEGRAM_OWNER_CHAT_ID` alanına yaz ve botu yeniden başlat.

Bot sadece bu chat id ile konuşur.

**Sürekli çalışsın** (oturum açılınca penceresiz başlar, çökerse yeniden başlar):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows-autostart.ps1           # kur
powershell -ExecutionPolicy Bypass -File scripts\windows-autostart.ps1 -Remove   # kaldır
```

## Telegram komutları

| Komut | Ne yapar |
|---|---|
| `/bugun` | Bugün ve yarın neler var |
| `/hafta` | Önümüzdeki 7 gün |
| `/takvim` | Önümüzdeki 30 gün |
| `/odevler` | Açık ödevler, aciliyet sırasıyla; ayrıntı butonları |
| `/notlar` | Notlar |
| `/duyurular` | Son duyurular |
| `/dosyalar [ders]` | Son materyaller; 📥 dosyayı Telegram'a gönderir |
| `/dersler` | Dersler, ilerleme, içerik ve ödev sayıları |
| `/yenile` | Siteyi hemen kontrol et |
| `/bildirimler` | **Uyarı yöneticisi:** sistem durumu (site erişimi, giriş, okunamayan bölümler), bildirim türlerini aç/kapa, erişim uyarısının eşiği (1/2/3/5 hata), gece modu, hızlı sessiz, son bildirimler |
| `/durum` | Son kontrol, sonraki kontrol, login kilidi, bekleyen bildirimler, LLM bütçesi |
| `/sessiz 2s` | Acil olmayanları 2 saat beklet (`30dk`, `1g`, `kapat`) |
| `/girisdene` | Login kilidini kaldır ve tekrar dene |
| `/unut` | LLM sohbet geçmişini sil |
| `/llmlog` | LLM son cevapta hangi araçla hangi veriye baktı; `/llmlog 3`, `/llmlog liste`, tam veri JSON olarak |

Bildirimlerin altındaki butonlar:
- **📄 Detay:** açıklamayı ve ekleri gösterir.
- **✅ Teslim ettim:** o ödevin hatırlatmalarını keser.
- **📥 Gönder:** materyali ya da eki Telegram'a gönderir.

📥 bir materyali açtığı için o içerik sitede "görüldü" sayılır.

Gece 01:00-07:00 arasında acil olmayan bildirimler sabaha bekletilir. Acil olanlar her zaman gelir: 3 saatten az kalan teslimler, canlı ders ve sistem uyarıları.

## LLM (isteğe bağlı)

`.env` içinde `LLM_PROVIDER` alanına `deepseek` ya da `xai`, `LLM_API_KEY` alanına da anahtarını yaz. `LLM_MODEL` boş bırakılırsa model otomatik seçilir; `doctor` mevcut modelleri listeler. LLM bağlanınca şunlar açılır:
- Serbest soru: "bu hafta neye odaklanayım?"
- Yeni ödevlere 3 maddelik özet ve tahmini süre.
- Sabah özetine "bugünün planı" bölümü.

Her LLM çağrısı (soru, çağrılan araçlar, gördüğü veri, cevap) kaydedilir ve `/llmlog` ile görülebilir. LLM sadece yerel veritabanını okuyan araçlar kullanır ve günlük token bütçesi vardır (`LLM_DAILY_TOKEN_BUDGET`). LLM çökse de bildirimler etkilenmez.

## CLI

| Komut | Ne yapar |
|---|---|
| `doctor` | Python, Playwright, tarayıcı, site, login formu, captcha, `.env`, Telegram ve LLM kontrolü; hiçbir şeyi değiştirmez |
| `bot` | Botu ve izlemeyi başlatır |
| `check [--dry-run]` | Tek tarama turu yapar; `--dry-run` sadece gösterir |
| `login [--headed] [--force]` | Giriş yapar ve oturumu kaydeder. `--headed` captcha için görünür tarayıcıda giriş yaptırır, `--force` login kilidini sıfırlar |
| `explore [--max-pages N]` | Siteyi sadece okuyarak keşfeder (HTML, ekran görüntüsü, JSON, HAR) |
| `setup-telegram` / `test-notify` | Chat id'leri listeler / deneme mesajı gönderir |
| `health` | Konteyner sağlık kontrolü (heartbeat) |

Veriler (`state.db`, `session.json`, loglar) Windows'ta `%LOCALAPPDATA%\ekampus`, Docker'da `/data` altında durur ve oturum çerezi içerdiği için ne repoya ne OneDrive'a girer.

**Login güvenliği:** Sunucu şifreyi bir kez reddederse aynı bilgilerle bir daha denenmez; hesap kilitlenmez ve captcha tetiklenmez. İki deneme arasında en az 10 dakika beklenir. Kilit, şifre değişince, `/girisdene` ile ya da `login --force` ile kalkar.

## Docker / bulut

```bash
docker compose build
docker compose run --rm ekampus python -m ekampus doctor
docker compose up -d
docker compose logs -f
```

Compose aynı `.env` dosyasını okur ve verileri `ekampus-data` volume'ünde tutar. İmaj amd64 ve arm64'te çalışır (Oracle free tier ARM dahil).

⚠️ Aynı anda tek bot çalışmalı: buluta geçince PC'deki görevi kaldır (`windows-autostart.ps1 -Remove`). İki bot birden çalışırsa Telegram çakışma hatası verir.

Sunucuda captcha çıkarsa PC'de `login --headed` ile giriş yap, sonra oturumu konteynere kopyala:

```bash
docker compose cp "$LOCALAPPDATA/ekampus/session.json" ekampus:/data/session.json
```

## Geliştirme

```powershell
.venv\Scripts\python -m pytest
```

Site yapısı ve tuzaklar: [docs/site-map.md](docs/site-map.md)
