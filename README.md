# Sunaar - Gold Loan Management (multi-shop)

Ek hi app, kayi jewellers. Har shop ka data, dashboard aur staff alag. Aap (super admin) shops banate, renew karte aur band karte ho.

## Kaun kya dekhta hai

| Role | Kya kar sakta hai |
|---|---|
| **Super admin** (aap) | Shops banana, subscription renew, shop band/chalu, owner ka password reset. Kisi shop ke loans nahin dekhta. |
| **Owner** (jeweller) | Apni shop ka poora dashboard, staff jodna/band karna, shop details, backup, loan delete. |
| **Staff** | Loan banana, payment lena, loan close karna, search. Dashboard par sirf apne banaye loans ke figures. |

Har loan par **"Banaya: <naam>"** likha rehta hai (detail page, loans list aur receipt par). Payment aur close karne wale ka naam bhi save hota hai.

## Local par chalana

```bash
pip install -r requirements.txt
python app.py
```

Browser: http://localhost:5000

Pehli baar super admin ban jata hai:
- Username: `superadmin`
- Password: `super123` (ya jo aap `SUPERADMIN_PASSWORD` mein do)

**Turant "Mera password" se badal dein.**

## Pehla customer (shop) jodna

1. Super admin se login karein.
2. **Nayi shop** par jaayein: shop ka naam, owner ka naam, username, password, subscription (mahine).
3. Owner ko username/password de dein. Wo login karke apna staff khud jod lega.

Subscription khatam hone par us shop ke sabhi users login nahin kar paate. **Renew** dabate hi dobara chalu.

## Customer khud sign-up kaise karega

Login page par **"Naye hain? Free account banayein"** ka link hai (`/signup`). Customer shop ka naam, apna naam, mobile, username aur password daalta hai, aur turant login ho jata hai. Use free trial milta hai (default 14 din).

Trial khatam hone se 7 din pehle owner ko upar chetavani dikhti hai. Khatam hone par login band ho jata hai aur aapka `SUPPORT_CONTACT` number dikhta hai. Payment aap apne tareeke se (UPI, bank, cash) lete ho, phir super admin panel mein **Renew** dabate ho. Khud sign-up kiye hue shops par admin panel mein "Khud sign-up kiya" likha aata hai.

Environment variables (sab zaroori nahin):

| Variable | Kaam | Default |
|---|---|---|
| `TRIAL_DAYS` | Free trial ke din | 14 |
| `SIGNUP_ENABLED` | `0` karne par sign-up band, sirf aap shops banao | 1 |
| `SUPPORT_CONTACT` | Renew ke liye aapka number, chetavani aur expire message mein dikhta hai | khaali |

Spam se bachav: chhupa hua honeypot field, aur ek network (IP) se ghante mein 5 se zyada account nahin ban sakte.

Local par chalate waqt ye aise set karein (Mac/Linux): `TRIAL_DAYS=30 SUPPORT_CONTACT="98765 43210" python3 app.py`

## Cloud par deploy (Render)

Data ko bachaane ke liye persistent disk zaroori hai (free tier par restart par data ud jata hai).

1. Code GitHub par daalein.
2. Render par **New > Blueprint** chunein, ye repo jodein (`render.yaml` apne aap padh liya jayega).
3. Dashboard mein `SUPERADMIN_PASSWORD` set karein.
4. Deploy ke baad aapko HTTPS link mil jayega.

Doosra option: PythonAnywhere (paid account par SQLite file aur uploads folder permanent rehte hain). `DATABASE_PATH` aur `UPLOAD_DIR` env variables se data ki jagah badli ja sakti hai.

## Zaroori baatein

- **Backup:** owner "Sabhi loans" page par "Backup download" se apni shop ka JSON backup le sakta hai. Aap server par `sunaar.db` aur `uploads/` folder ka regular backup lein.
- **Byaj ka hisaab:** simple interest, prati mahina dar, din ke hisaab se (30 din = 1 mahina). Agar aapke customers ka niyam alag hai (jaise kam se kam 1 mahine ka byaj, ya chakravriddhi), `calc_interest` function `app.py` mein badalna hoga.
- **Scale:** SQLite kuch dozen shops ke liye theek hai. Isse zyada shops hon to PostgreSQL par jaana behtar rahega.
- **Security:** login par CSRF suraksha, password hashed, har query shop ke hisaab se filter hoti hai, photos sirf usi shop ke logged-in users ko dikhti hain. Production mein hamesha HTTPS use karein aur `SECRET_KEY` apna rakhein.

## Files

```
app.py              Flask app (routes, database, roles)
static/css/app.css  Poora design
templates/          Pages
requirements.txt    Packages
Procfile, render.yaml   Deploy
```
