# Xray Jira Test Otomasyon Aracı — İstekler ve Karşılanma Durumu

**Tarih:** 19.08.2026
**Branch:** `claude/xray-jira-test-automation-cdhf2j`
**Jira:** https://jira.thy.com (Jira Server/Data Center + Xray)

---

## 1. İstenenler

| # | İstek | Durum | Nasıl karşılandı |
|---|-------|-------|------------------|
| 1 | Uygulama ve arayüz | ✅ | Flask tabanlı web uygulaması + tek sayfalık Türkçe arayüz (`app.py`, `templates/index.html`) |
| 2 | 20 satıra tek tek test senaryosu girme | ✅ | Arayüzde 20 satırlık tablo: Senaryo Adı, Açıklama, Adımlar sütunları |
| 3 | Xray Jira API entegrasyonu | ✅ | Jira REST + Xray Raven API (aşağıdaki endpoint tablosu) |
| 4 | Senaryoları otomatik create etme | ✅ | Her dolu satır için `Test` issue'su oluşturulur, adımlar Xray manuel adımı olarak eklenir |
| 5 | Test Set'e bağlama | ✅ | Yeni Test Set oluşturur **veya** mevcut Test Set key'ine bağlar |
| 6 | Execution oluşturma | ✅ | Test Execution otomatik oluşturulur, tüm testler eklenir |
| 7 | PASS'leme | ✅ | Execution içindeki tüm testler `PASS` işaretlenir (istenirse FAIL/TODO seçilebilir) |
| 8 | PAT'in ilk açılışta sorulması | ✅ | İlk açılışta kurulum penceresi çıkar: Jira URL + PAT + Proje anahtarı. PAT yalnızca yerelde `config.json`'da saklanır (git'e girmez) |

## 2. Akış (tek tuş: "🚀 Oluştur, Bağla ve PASS'le")

1. Tablodaki her dolu satır için **Test** issue'su oluşturulur → satırda `✅ PROJ-123` görünür
2. Adımlar (`adım => beklenen sonuç` formatı) Xray manuel test adımı olarak eklenir
3. Tüm testler **Test Set**'e bağlanır (yeni ad girilir veya mevcut key kullanılır)
4. **Test Execution** oluşturulur ve tüm testler **PASS** işaretlenir
5. Tüm işlemler alttaki log panelinde canlı izlenir

## 3. Kullanılan API'ler

| İşlem | Endpoint |
|---|---|
| Bağlantı doğrulama | `GET /rest/api/2/myself` (Bearer PAT) |
| Test / Test Set issue oluşturma | `POST /rest/api/2/issue` |
| Test adımı ekleme | `POST /rest/raven/2.0/api/test/{key}/steps` (eski Xray için fallback: v1 `PUT .../step`) |
| Test Set'e test bağlama | `POST /rest/raven/1.0/api/testset/{key}/test` |
| Execution oluşturma + PASS | `POST /rest/raven/1.0/import/execution` (fallback: `testexec` + `testrun/status`) |

## 4. Kurulum ve Çalıştırma (THY ağındaki bilgisayarda)

```bash
git clone -b claude/xray-jira-test-automation-cdhf2j https://github.com/mkonay1/Deneme.git
cd Deneme
pip install -r requirements.txt
python app.py
```

Tarayıcıda **http://127.0.0.1:5000** → ilk kurulum penceresine:

- **Jira URL:** `https://jira.thy.com`
- **PAT:** Jira profilinden (Profil → Personal Access Tokens) oluşturulan token
- **Proje Anahtarı:** çalışılan projenin key'i (örn. `PROJ`)

"Kaydet ve Bağlan" bağlantıyı anında doğrular. Kurumsal sertifika hatasında gelişmiş ayarlardan SSL doğrulama kapatılabilir; issue tipi adları farklıysa (Türkçe Jira) gelişmiş ayarlardan değiştirilebilir.

## 5. Notlar

- **Güvenlik:** Chat'e yapıştırılan PAT iptal edilip yenisi oluşturulmalı. Yeni PAT yalnızca uygulamanın kurulum ekranına girilmeli.
- **Bağlantı testi:** `jira.thy.com` kurum içi ağda olduğundan bulut ortamından erişilemedi; gerçek doğrulama THY ağındaki bilgisayardan yapılmalı.
- Uygulama, sahte (mock) bir Jira/Xray sunucusuna karşı uçtan uca test edilmiş ve tüm akış doğrulanmıştır.
