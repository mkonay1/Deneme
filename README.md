# Xray Test Otomasyon Aracı

Web arayüzünden 20 satıra kadar test senaryosu girip tek tuşla Jira/Xray'e aktaran araç:

1. Her satır için **Test** issue'su oluşturur (manuel adımlarıyla birlikte)
2. Testleri bir **Test Set**'e bağlar (yeni oluşturur veya mevcut key kullanılır)
3. **Test Execution** oluşturur ve tüm testleri **PASS** olarak işaretler

Kimlik doğrulama Jira **Personal Access Token (PAT)** iledir (Jira Server / Data Center, Bearer). PAT uygulama ilk açıldığında arayüzde sorulur ve yalnızca yerelde `config.json` dosyasına kaydedilir (git'e girmez).

## Kurulum

```bash
pip install -r requirements.txt
python app.py
```

Tarayıcıda: http://127.0.0.1:5000

## İlk açılış

Açılışta kurulum penceresi gelir:

- **Jira URL** — örn. `https://jira.sirketiniz.com`
- **PAT** — Jira profilinizden (Profil → Personal Access Tokens) oluşturduğunuz token
- **Proje Anahtarı** — örn. `PROJ`

Gelişmiş ayarlardan issue tipi adlarını (`Test`, `Test Set`, `Test Execution` — Türkçe Jira'da farklıysa) ve SSL doğrulamayı değiştirebilirsiniz. "Kaydet ve Bağlan" tuşu bağlantıyı `/rest/api/2/myself` ile doğrular.

## Kullanım

1. Tabloya senaryolarınızı girin. Adımlar sütununda her satır bir adımdır; `=>` işaretinden sonrası beklenen sonuçtur:
   `Kullanıcı giriş yapar => Ana sayfa açılır`
2. Test Set adı (veya mevcut Test Set key'i) ve Execution adını girin.
3. **"Oluştur, Bağla ve PASS'le"** butonuna basın. Her satırın durumu tabloda, tüm akış alttaki log panelinde canlı görünür.

## Kullanılan API'ler

| İşlem | Endpoint |
|---|---|
| Test / Test Set issue oluşturma | `POST /rest/api/2/issue` |
| Test adımı ekleme | `POST /rest/raven/2.0/api/test/{key}/steps` (fallback: v1 `PUT .../step`) |
| Test Set'e test bağlama | `POST /rest/raven/1.0/api/testset/{key}/test` |
| Execution oluşturma + PASS | `POST /rest/raven/1.0/import/execution` (fallback: `testexec` + `testrun/status`) |
