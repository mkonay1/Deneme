# Xray Test Koşum Paneli — Geliştirme Dokümanı

> jira.thy.com üzerindeki Xray test planı/execution verilerini kişi–gün–saat–statü
> kırılımlarıyla görselleştiren, bug akışını izleyen yerel web paneli.
> Konum: `~/Desktop/Sunumlar/xray-panel/` · Dağıtım: `xray-panel.zip` (indir-çalıştır)
> Geliştirme dönemi: 8 Temmuz – 17 Ağustos 2026

---

## 1. Ne İşe Yarar

Tek ekranda üç bölüm (sekme):

| Sekme | İçerik |
|---|---|
| 🧪 **Test Koşumları** | Özet kartları, Otomatik Analiz, plan/execution ilerlemeleri, günlük trend, Kişi × Gün ısı haritası (statü filtreli), saatlik ısı haritası, kişi bazında koşum, Kişi × Execution detayı |
| 🐞 **Bug Analizi** | Açılan Bug — Kişi × Gün matrisi, Bug Retest Takibi (açık kuyruklar + işlem görenlerin son statüleri), Bug Statü Süreleri (changelog analizi) |
| 📋 **Açık Bug Stoku** | Projedeki çözülmemiş tüm bug'lar: statü/atanan/yaş dağılımları, en yaşlı 15, öncelik özeti |

Çıktılar: **CSV** (tüm tablolar + otomatik analiz maddeleri) ve **PPTX**
(qkg-haftalik-rapor görsel dilinde 2 slayt: dashboard + Kişi × Gün matrisi).

## 2. Mimari

```
tarayıcı (index.html, tek dosya UI)
        │  fetch /api/...
        ▼
server.py (Python stdlib, bağımlılıksız — python-pptx yalnız PPTX için)
        │  Bearer PAT
        ▼
jira.thy.com  (Jira Server 9.12 + Xray/Raven REST)
```

- **CORS çözümü:** Tarayıcı Jira'ya doğrudan çıkamaz; yerel sunucu köprü görevi görür.
- **Kimlik:** PAT token — panel her açılışta arayüzdeki giriş ekranında istenir, yalnızca
  oturum belleğinde tutulur (diske yazılmaz). İsteğe bağlı override: `JIRA_TOKEN` env.
  *(26. madde ve aşağıki dosya listesi güncellendi — önceki sürümde `token.txt`/`~/.jira_token`
  dosyasından okunuyordu.)*
- **Portatif paket:** `calistir.bat` (Windows), `calistir.command` (mac),
  `pptx_kur_offline.bat` + `wheels/` (python-pptx'in internetsiz kurulumu, Win64/Py3.12),
  `KURULUM.txt`.

### Kullanılan Jira/Xray API'leri

| Amaç | Endpoint |
|---|---|
| Kimlik doğrulama | `/rest/api/2/myself` |
| Planın execution listesi | `/rest/raven/1.0/api/testplan/{plan}/testexecution` |
| Execution'ın test koşumları | `/rest/raven/1.0/api/testexec/{key}/test?detailed=true` (sayfalı, 200'lük) |
| Plan ilerlemesi | `/rest/raven/1.0/api/testplan/{plan}/test` (latestStatus) |
| Projedeki tüm execution'lar | JQL `issuetype = "Test Execution"` (en yeni 300 sınırı) |
| Açılan bug'lar | JQL `issuetype = Bug AND reporter in (ekip) AND created ...` |
| Retest kuyruğu | JQL `assignee in (ekip) AND resolution is EMPTY` |
| Retest aktivitesi | JQL `status CHANGED BY "kişi" DURING (aralık)` |
| Bug statü süreleri | JQL + `expand=changelog` (50'lik sayfalar, en yeni 500 bug) |
| Açık bug stoku | JQL `resolution is EMPTY` (1000 sınırı) |
| Kullanıcı bilgisi | `/rest/api/2/user?username=` → `?key=` (displayName + **active** bayrağı) |

## 3. Hız Mimarisi (üç katmanlı önbellek)

1. **`exec_cache.json` — kalıcı execution önbelleği (v2).** Her execution'ın tüm koşum
   verisi (gün+saat+kişi+statü) diskte. Her sorguda tek toplu JQL ile `updated`
   damgaları kontrol edilir; yalnız **değişen VEYA aktif (TODO'su olan) VEYA 24 saatten
   eski** kayıtlar yeniden taranır. Tarih aralığı filtresi önbellek üzerinde uygulanır —
   aralık değiştirmek tarama gerektirmez. "Tam tarama" düğmesi (`full=1`) önbelleği atlar.
2. **`results_cache.json` — kalıcı sonuç önbelleği (son 20 sorgu).** Sunucu kapanıp
   açılsa bile son görünüm anında döner. 10 dk TTL geçmişse: önce kayıtlı veri gösterilir
   (💾 notuyla), arka planda taze veri çekilir (stale-while-revalidate).
3. **Ekip kaydı (`teams`, exec_cache içinde).** Hedef başına şimdiye dek görülen TÜM
   koşucular birikir — 300'lük proje sınırı dışında kalan ya da üzerine yeniden koşum
   yapılan execution'ların koşucuları kaybolmaz; bug/retest taramaları bu listeyle yapılır.

Ölçülen etki: 22 execution'lık çift plan sorgusu 71 sn → 43 sn; tekrar eden sorgu 0,01 sn.
86+ execution'lık proje taraması ilk sefer ~4-7 dk, sonrası büyük oranda önbellekten.

## 4. Özellik Geçmişi (kronolojik)

1. **Temel panel** — PowerShell prototipinin web'e taşınması: plan tarama, kişi bazında
   günlük koşum sayısı, koyu/açık tema.
2. **Rate-limit dayanımı** — Jira 429 için exponential backoff (Retry-After'a saygılı),
   paralellik 3 worker'a sabitlendi.
3. **Zengin metrikler** — tarih aralığı + günlük trend, PASS/FAIL kırılımı, 10 dk yanıt
   önbelleği + oto-yenileme, plan ilerlemesi + bitiş tahmini, CSV, PPTX, plan seçici.
4. **Execution ilerlemeleri** — her execution ayrı satır: %, koşulan/toplam, statü barı.
5. **İndir-çalıştır paketi** — bat/command başlatıcılar, offline wheels, KURULUM.txt.
6. **Çoklu hedef** — virgülle birden fazla plan; plan bazında ilerleme kartı.
7. **Kişi × Gün matrisi + gerçek Jira statüleri** — Başarılı/Başarısız gruplaması yerine
   PASS/FAIL/EXECUTING/ABORTED/BLOCKED... dinamik renk + lejant; günlük ort. / en yoğun gün.
8. **Bug takibi** — reporter bazlı Açılan Bug (Kişi × Gün, kırmızı ısı matrisi).
9. **Proje taraması** — kutuya proje key'i yazılınca projedeki TÜM execution'lar
   (en yeni 300; sınır aşımı notla bildirilir).
10. **Ağ dayanımı** — Connection reset / yarım yanıt durumlarında da retry;
    kopan istemci bağlantısında sessiz geçiş.
11. **Artımlı önbellek + changelog kontrolü** (bkz. Hız Mimarisi).
12. **BLOCKED koşumlar** — bitiş tarihi olmayan bloke koşumlar `startedOn` ile sayılır.
13. **Kişi × Execution detayı** — akordeon: kişinin hangi execution'da ne koştuğu.
14. **Tarih girişi düzeltmesi** — native date input gömülü tarayıcıda çalışmadığından
    serbest metin (gg.aa.yyyy; gg/aa/yyyy ve ISO da kabul).
15. **Otomatik Analiz** — kural tabanlı yorum kartı: koşum/retest ağırlığı, PASS/BLOCKED
    oranları, liderler, mesai dışı zirve tespiti, işlem görmemiş kuyruk uyarıları,
    %90+ ve %10 altı takılı execution'lar; CSV'ye de eklenir.
16. **Retest Takibi** — assignee bazlı açık bug kuyruğu (Jira iş akışı statüleriyle) +
    aralıkta statü değiştirdiği bug sayısı + işlem görenlerin **son statüleri**.
17. **Pasif hesap filtresi** — Jira `active=false` hesaplar retest takibinden elenir
    (örn. E_KUL aktif, eski RDCEKUL hesabı elenir).
18. **Ekip kaydı** — koşucu keşfi birikimli; kimse kaçmaz.
19. **Sonuç kalıcılığı** — results_cache.json + stale-while-revalidate.
20. **Saatlik ısı haritası** — kişi × günün saati (exec cache v2 ile).
21. **Isı haritası statü filtresi** — Tümü/PASS/FAIL/BLOCKED... chip'leri; seçilen
    statünün rengiyle boyanır; PASS/FAIL dışındaki statüler için dinamik KPI kartları.
22. **Bug Statü Süreleri** — changelog'dan statü başına ort/toplam süre; kapalı
    statülerde sayaç durur; en uzun süredir süreçte olanlar; bulgular Otomatik
    Analiz'e madde olarak eklenir. Kapsam: projedeki tüm bug'lar (en yeni 500).
23. **Sekmeler** — Test Koşumları / Bug Analizi / Açık Bug Stoku (seçim hatırlanır).
24. **Açık Bug Stoku** — resolution is EMPTY: statü/atanan/yaş/öncelik dağılımları,
    en yaşlı 15, CSV'de tam liste.
25. **Dayanıklılık** — UI isteklerinde 15 dk zaman aşımı (AbortController); Getir yalnız
    ön plan sorgusunda kilitlenir; demo modunda belirgin uyarı şeridi.
26. **Token'ı diske yazmama** — token artık `token.txt`/`~/.jira_token`'a kaydedilmiyor;
    yalnızca sunucu belleğinde (`_session_token`) tutuluyor. Panel her açılışta arayüzdeki
    giriş ekranından (`POST /api/token`, `/rest/api/2/myself` ile doğrulanır) token ister;
    durum rozetine tıklayarak değiştirilebilir. Başlatıcılar artık token sormuyor/yazmıyor.

## 5. Önemli Teknik Bulgular (bir daha düşülmesin diye)

- **Jira tarih formatı:** `created`/`finishedOn` alanları `+0300` (iki noktasız) offset
  döndürür; Python 3.9 `fromisoformat` bunu **ayrıştıramaz** → `%z`'li strptime şart.
  (İlk bug sayımının 0 çıkmasının sebebi buydu.)
- **Xray `updated` tuzağı:** Koşum statüsü değişimi execution issue'sunun `updated`
  damgasını her zaman değiştirmez → "tamamlanmış" önbellek kayıtları bayatlayabilir.
  Çözüm: 24 saatlik zorunlu tazeleme. (BLOCKED/PASS sayılarının hatalı görünmesinin sebebi.)
- **Xray son-koşucu sınırı:** `/testexec/{key}/test` her test için yalnız **son** koşumu
  gösterir; bir test yeniden koşulunca önceki koşucunun izi silinir → ekip kaydı birikimli
  tutulur.
- **Plan kapsamı ≠ proje kapsamı:** Plana bağlanmamış execution'daki koşumlar plan
  sorgusunda görünmez (Elif Nur vakası: planda 0, projede 107 koşum). Kesin sonuç için
  proje key'iyle sorgula ya da execution'ları Jira'da plana bağla.
- **Gömülü tarayıcı + native date input:** Takvim popover'ı native bileşen olduğundan
  panel içinde çalışmaz → serbest metin tarih girişi.
- **Mac uykusu:** Uyku sonrası sunucu süreci kilitlenebilir (port dinlemede ama yanıt
  yok) → süreci öldürüp yeniden başlat; UI tarafı 15 dk zaman aşımıyla kendini korur.
- **Jira ağ davranışı:** Uzun taramalarda ara sıra `Connection reset by peer` → tüm ağ
  hatalarında backoff'lu retry (yalnızca 429 değil).
- **Rate limit:** 3 paralel worker güvenli; execution tarama sayfaları 200'lük.

## 6. Dosyalar

```
xray-panel/
├── server.py            # yerel sunucu + Jira köprüsü (stdlib, Python 3.9+)
├── index.html           # tüm arayüz (tek dosya: CSS+JS)
├── pptx_report.py       # PPTX üretici (python-pptx; isteğe bağlı)
├── calistir.bat         # Windows başlatıcı (token'ı sorar)
├── calistir.command     # macOS/Linux başlatıcı
├── pptx_kur_offline.bat # python-pptx offline kurulum
├── KURULUM.txt          # kurulum ve kullanım notları
├── wheels/              # Win64/Py3.12 offline paketler
├── exec_cache.json      # execution önbelleği + ekip kaydı (otomatik)
└── results_cache.json   # sorgu sonucu önbelleği (otomatik)
```

## 7. API Özeti (yerel sunucu)

| Endpoint | Ne döner |
|---|---|
| `GET /api/status` | Token/bağlantı durumu + kullanıcı adı |
| `GET /api/plans?project=X` | Projedeki test planları (öneri listesi) |
| `GET /api/runs?plan=&from=&to=` | Ana veri seti (koşumlar, kişiler, matrisler, bug'lar, retest, analizler) — `refresh=1` yanıt önbelleğini, `full=1` tümünü atlar, `demo=1` sahte veri |
| `GET /api/report.pptx?...` | 2 slaytlık PPTX |
| `GET /api/bugtime?plan=` | Bug statü süreleri (changelog analizi) |
| `GET /api/openbugs?plan=` | Açık bug stoku |

## 8. Kayda Değer Analiz Bulguları (geliştirme sırasında gerçek veriden)

- Bug yaşam süresinin **~%94'ü bekleme kuyruklarında** (Open ort. 6-9,5 gün; On Hold ort.
  28,6 gün) geçiyor; fiili geliştirme ~3 gün, QA retest'i ~3 saat.
- Açık stok (116 bug): %68'i Open+On Hold'da; 27'si 90 günden yaşlı; en yaşlı seri
  "Amadeus vs Mercury Itenary" (13 bug, 170-236 gün, tek kök neden adayı);
  400+ günlük iki `getIssueServiceFee` kaydı temizlik bekliyor.
- Ekipte iki çalışma deseni: gündüz manuel koşum (08-16) ve gece otomasyon (20-24).
- Önceliklendirme zayıf: 116 açık bug'ın 109'u "Medium".

---
*Bu doküman panel geliştirme oturumlarının özetidir — güncel davranış için KURULUM.txt
ve kodun kendisi esas alınmalıdır.*
