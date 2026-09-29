# Smart Tech AI CCTV – Naye PC par setup

Yeh guide shuru se aakhir tak hai. Takreeban **30–45 minute** lagte hain (zyada tar downloads).
Do tarah ke PC ke liye steps hain:

- **A. Windows 10/11** (Docker Desktop) – testing / chhote setup ke liye
- **B. Ubuntu 24.04 (Intel N100 mini PC)** – asal customer box, behtar aur tez (Intel GPU istemal hota hai)

Jo cheezein GitHub par **nahi** hain (aur alag se chahiye): `.env` passwords, Firebase key,
YOLOv9 model file. Yeh sab neeche steps mein banti hain.

---

## 1. Zaroori software install karein

### A. Windows
1. **Docker Desktop**: https://www.docker.com/products/docker-desktop → install → restart.
   - Settings → General → ✅ *Start Docker Desktop when you sign in*
   - Settings → Resources → Memory kam az kam **6 GB**
2. **Git**: https://git-scm.com/download/win (sab default options theek hain)
3. **Sirf agar PC ka webcam test karna ho**: PowerShell kholein aur
   ```powershell
   winget install Gyan.FFmpeg
   ```

### B. Ubuntu 24.04 (N100)
```bash
sudo apt update && sudo apt install -y git curl
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER      # phir logout / login karein
sudo systemctl enable docker       # restart par Docker khud chale
```

Check: `docker --version` aur `docker compose version` dono chalne chahiye.

---

## 2. Code download karein

```bash
git clone https://github.com/MohsinALi1a1/CCTV_SECURITY.git
cd CCTV_SECURITY
```
(Windows par yeh PowerShell ya Git Bash mein chalayein. Folder koi bhi ho sakta hai, maslan Desktop.)

---

## 3. Settings file (`.env`) banayein

```bash
cp .env.example .env          # Windows PowerShell:  copy .env.example .env
```
`.env` ko Notepad / nano se kholein aur badlein:

| Setting | Kya likhein |
|---|---|
| `TZ` | `Asia/Karachi` (ya customer ka time zone) |
| `MQTT_PASSWORD` | koi bhi lamba random password (maslan 20 characters) |
| `HOME_ID` | har ghar ka **alag aur mushkil** id, maslan `home_7f3k9q2x` (mobile push topic isi se banta hai) |
| `HOME_NAME` | maslan `Ahmed House` |
| `LAN_SUBNET` | customer ka network, maslan `192.168.1.0/24` (router ke IP se pata chalta hai) |
| `FIREBASE_ENABLED` | mobile app chahiye to `true`, warna `false` |

`RTSP_USER` / `RTSP_PASSWORD` ki zaroorat nahi – cameras dashboard se add hote hain.

⚠ `.env` kabhi GitHub par ya kisi ko na bhejein.

---

## 4. Secret folders banayein

```bash
mkdir -p secrets/cameras data
```
(Windows PowerShell: `mkdir secrets\cameras, data`)

**Mobile app (Firebase) chahiye to:** Firebase console → Project settings → Service accounts →
*Generate new private key* → download hui JSON file ko yahan rakhein:
```
secrets/firebase-service-account.json
```
Mobile app nahi chahiye to yeh step chhor dein (engine khud Firebase band kar deta hai).

---

## 5. AI model (YOLOv9) banayein – sirf pehli dafa

Model file GitHub par nahi hoti (bari hai). Project folder mein chalayein:

```bash
docker build scripts -f scripts/yolov9_export.Dockerfile --build-arg MODEL_SIZE=s --build-arg IMG_SIZE=320 --output frigate/config/model_cache
```
10–20 minute lagte hain. Aakhir mein yeh file honi chahiye:
`frigate/config/model_cache/yolov9-s-320.onnx`

> Kamzor PC (N100 bina GPU ke) ke liye `MODEL_SIZE=t` banayein aur `frigate/config/config.yml`
> mein `path:` ko `yolov9-t-320.onnx` kar dein.

---

## 6. Purana test camera hatayein (naye ghar mein zaroori)

Repo mein development wala **PC webcam** (`cam01_pc`) aur us ke test areas hain. Naye ghar mein:

1. `frigate/config/config.yml` kholein:
   - `go2rtc: streams:` ke neeche `cam01_pc:` aur us ki line delete karein
   - `cameras:` ke neeche poora `cam01_pc:` block delete karein
     (lekin `cameras:` ke neeche kam az kam **ek camera zaroori hai** – agar abhi koi camera nahi
     to webcam block rehne dein, asli camera add karne ke baad hata dein)
2. `event_engine/config/rules.yml` mein `cameras:` ke neeche `cam01_pc:` wala hissa delete karein.
3. `docker-compose.yml` mein `rtsp-server` service sirf webcam ke liye hai – asli cameras ke saath
   isay delete kar sakte hain (Frigate ke `depends_on` se bhi `rtsp-server` hata dein).

---

## 7. Intel GPU on karein (sirf Ubuntu N100 par)

1. `docker-compose.yml` mein `frigate:` ke neeche yeh 2 lines uncomment karein:
   ```yaml
   devices:
     - /dev/dri/renderD128:/dev/dri/renderD128
   ```
2. `frigate/config/config.yml` mein:
   ```yaml
   detectors:
     ov:
       type: openvino
       device: GPU          # CPU ki jagah
   ffmpeg:
     hwaccel_args: preset-vaapi
   ```
   (`ffmpeg:` wala hissa file ke upar, `mqtt:` ke saath rakhein.)
3. Face recognition ke liye GPU par `model_size: large` bhi kar sakte hain (zyada accurate).

---

## 8. System start karein

```bash
docker compose up -d --build
```
Pehli dafa 5–10 minute (Frigate image ~1.5 GB download hoti hai). Check:
```bash
docker ps
```
Chaar containers `Up` hone chahiye: `frigate`, `mosquitto`, `smarttech-engine`, `rtsp-server`
(agar aap ne hataya nahi).

---

## 9. Pehla login aur test

1. **Dashboard:** http://localhost:8080 → *Allow notifications* → **Test alert** dabayein
   (laal banner + awaz + notification aani chahiye).
2. **Frigate (advanced):** https://localhost:8971 → certificate warning par *Advanced → Continue*.
   User `admin`, password yeh command dikhayegi:
   ```bash
   docker logs frigate 2>&1 | grep "Password:"
   ```
   (Windows PowerShell: `docker logs frigate 2>&1 | Select-String "Password:"`)
   Login ke baad *Settings → Users* mein password badal dein.

---

## 10. Cameras add karein (dashboard se)

1. Camera ko router se jorein (ya camera ki app se Wi-Fi par), camera ki app mein admin password rakhein
   aur RTSP/ONVIF on karein (Tapo/EZVIZ mein zaroori).
2. Router mein camera ko **fixed IP** (DHCP reservation) dein.
3. Dashboard → **Add camera** → *Scan for cameras* → camera chunein → username/password →
   *Connect* → tasveer check karein → naam, **Outside / Inside**, face recognition → **Add camera**.
4. Aakhir mein **Draw areas** → deewar / gate par 🚫 No-entry ya ⚠ Restricted area draw karein.

---

## 11. Family ke chehre add karein

Frigate → **Face Library** → *Add Face* → naam → 5–10 saaf, saamne se, colour photos.
Kuch din baad *Train* tab mein camera ki asli tasveerein bhi sahi naam par lagayein.

---

## 12. Mobile app (Firebase) – ek dafa

Sirf agar step 4 mein Firebase key rakhi:
1. Firebase console → **Firestore Database** bana hua ho (production mode).
2. **Rules** tab → `firebase/firestore.rules` ka content paste → Publish.
3. **Authentication** mein customer ka user banayein → us ka **UID** copy karein.
4. Firestore → `homes/<HOME_ID>` document → field `members` (array) mein UID add karein.
5. App mein push topic: `smarttech_<HOME_ID>`.

Detail: `docs/MOBILE.md`.

---

## 13. (Optional) PC webcam se test

Sirf Windows par, jab asli camera na ho:
```powershell
.\scripts\start_webcam.ps1 -ListCameras                 # webcam ka naam
.\scripts\install_webcam_autostart.ps1 -Camera "Integrated Webcam"   # login par khud chale
```
Webcam ka naam `-Camera` mein wahi likhein jo pehli command ne dikhaya.

---

## Rozana / maintenance

| Kaam | Command |
|---|---|
| Naya code lena (update) | `git pull` phir `docker compose up -d --build` |
| Sab band | `docker compose down` |
| Sab chalu | `docker compose up -d` |
| Engine ke logs | `docker logs -f smarttech-engine` |
| Frigate ke logs | `docker logs -f frigate` |
| Kisi shakhs ka data delete | `.\scripts\delete_person.ps1 -Name "Ahmed"` (Windows) |

## Backup – PC badalne se pehle yeh copy karein

| Folder / file | Kya hai |
|---|---|
| `.env` | passwords, home id |
| `secrets/` | Firebase key + camera logins |
| `frigate/config/config.yml` | cameras aur areas |
| `event_engine/config/rules.yml` | alert rules |
| `data/` | alerts history, areas, cameras list |
| `frigate/storage/clips/faces/` | family ke chehre |
| `frigate/config/model_cache/` | AI model (dobara banane se bachne ke liye) |

Naye PC par: step 1–2 karein, phir yeh sab usi jagah copy karein, phir step 8.

## Aam masle

| Masla | Hal |
|---|---|
| `docker` command nahi milti | Docker Desktop khula hai? Ubuntu: logout/login kiya `usermod` ke baad? |
| Dashboard nahi khulta | `docker ps` – `smarttech-engine` chal raha hai? `docker logs smarttech-engine` |
| "Frigate not responding" | `docker logs frigate` – aksar `config.yml` ki ghalti ya model file missing (step 5) |
| Scan mein camera nahi aata | Camera aur PC ek hi network par? `LAN_SUBNET` sahi? IP khud likh kar try karein |
| "Could not open the camera video" | Password ghalat, ya camera ki app mein RTSP band |
| Mobile par "offline" | `homes/<HOME_ID>.members` mein app user ka UID hai? Firestore rules publish kiye? |
| Mosquitto baar baar restart | `docker compose up -d mosquitto` dobara chalayein (password file khud dobara banti hai) |
