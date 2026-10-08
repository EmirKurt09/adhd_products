# e-Kampüs site haritası (Toltek TCampus)

Keşif tarihi: 2026-10-09. Kişisel veri içermez. Ayrıştırıcılar `ekampus/parse.py` içinde, test fixture'ları `tests/fixtures/` altında.

## Kimlik doğrulama

- Öğrenci sitesi `https://student.ekampus.ticaret.edu.tr`, ASP.NET Core ile çalışıyor ve girişi OIDC istemcisi olarak `toltek.campus.student` üzerinden yapıyor.
- Oturum yokken her sayfa 200 dönüyor ve `name="hiddenform"` içeren "Working..." sayfası geliyor. Bu sayfa kendiliğinden `https://ekampus.ticaret.edu.tr/connect/authorize` adresine POST atıyor. `max_age=86400` olduğu için en geç günde bir login gerekiyor.
- Login sayfası `/Account/Login` adresinde. Form alanları `Username`, `Password`, `RememberMe` ve `__RequestVerificationToken`; gönderim butonu `#btnSubmit`.
  - Başarılı girişte sunucu 302 dönüyor; giriş reddedilirse form 200 ile yeniden çiziliyor.
  - reCAPTCHA `api.js` sayfaya yükleniyor ama forma bağlı değil. Muhtemelen hatalı denemelerden sonra devreye giriyor.
- Sayfalar JavaScript gerektirmiyor. Tarayıcı bağlamının `request` API'siyle, aynı çerezler kullanılarak çekiliyor.

## Okunan sayfalar

| Adres | İçerik | Ayrıştırıcı |
|---|---|---|
| `/Course` | Ders tablosu. `a[href^=/Course/Details/][title=KOD]`, ilerleme için `[data-percent]`; sayılar popover'da (`data-bs-title` / `data-bs-content`: "Toplam : 5 <br/> İzlenen : 1") | `parse_course_list` |
| `/Course/Details/{id}` | Kartlar: içerik bölümleri (ör. "Genel (5)", "4.Hafta"), **Eğitim Bilgileri**, **Aktiviteler** (rozetler: "Teslim Edildi", "Sonuç : 99,00") | `parse_course_detail` |
| `/Schedule/Data?start=ISO&end=ISO` | JSON listesi: `CourseName, ScheduleName, TypeName, Start, End` (saat dilimsiz, İstanbul), `Description` (HTML), `Url`. Sadece aralıkla çakışanlar gelir. | `parse_schedule` |
| `/Announce/Refresh` → `/Announce` | Duyuru listesi. Boşken "Duyuru kaydı bulunamadı" yazıyor. **Dolu hali henüz görülmedi**; yapı tanınmazsa canary uyarı veriyor. | `parse_announcements` |
| `/assignment/details/{id}` | Sadece istek üzerine açılıyor: açıklama (`.card-body .alert`), ekler (`storage.ekampus.../Files/Uploads/.../Assignment/...`), "Teslim Tarihi" (göreli metin: "Bugün 13:00"), Limit, Durum | `parse_assignment_detail` |

## Tuzaklar

- **`/content/enroll/{icerikId}?ceid=..&GroupId=..`** içeriği "görüldü" işaretliyor ve ilerleme yüzdesini değiştiriyor. Ardından `/Content/Details/{kayitId}` adresine yönlendiriyor. Bu yüzden içerik linkinin ve ID'sinin, içerik açılınca değiştiğini unutma. Materyalin kimliği bu yüzden `ders + bölüm + başlık` üzerinden kuruluyor. Tarama bu adresi hiç açmaz; sadece "📥 Gönder" butonuna basılınca açılır.
- `/home/sync` dersleri ÖBS ile eşitliyor; açılmaz.
- Takvimde yalnızca teslim tarihi olan ödevler görünüyor. Süresiz ödevler sadece ders sayfasında yer alıyor; sonradan tarih eklenirse "teslim tarihi eklendi" olayı üretiliyor.
- Sanal sınıf ve sınav türleri bu dönem henüz görülmedi. Takvimdeki `TypeName` değeri `LIVE_TYPES` listesine uyuyorsa canlı ders, uymuyorsa genel etkinlik olarak işleniyor, yani hiçbiri kaçmıyor.
- Ders dosyaları `storage.ekampus.ticaret.edu.tr/Files/Uploads/ticaret/Course/{n}/...` altında duruyor; içerik sayfasında pdf.js iframe'inin `?file=` parametresinde yer alıyor.
