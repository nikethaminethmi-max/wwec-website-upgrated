# WWEC Website – Full Backend, Google Sheets, Google Drive & Admin Dashboard

Complete Flask web application for **World Wide Engineering & Constructions (Pvt) Ltd (WWEC)**.

## Features
- **Existing Website Preserved**: All 16 HTML pages, Tailwind CSS config, light/dark mode, animations, and images in `public/` remain intact.
- **Customer Contact Form (`/api/contact`)**: Validates inputs, blocks spam/duplicates, and saves every enquiry directly to Google Sheets (`Enquiries` tab).
- **Google Drive Project Gallery (`/api/gallery`)**: Staff upload project photos (JPG, JPEG, PNG, WEBP) from the Admin Dashboard; images are stored in Google Drive, metadata is saved in Google Sheets (`Gallery` tab), and new projects appear automatically on `gallery.html`.
- **Private Admin Dashboard (`/admin`)**: Protected by username + PBKDF2 hashed password login, session cookies, rate limiting, and CSRF tokens.

## Quick Local Setup
```bash
python -m venv venv
# Windows:
venv\Scripts\activate
# Mac/Linux:
source venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
python scripts/generate_admin_hash.py
# Paste generated values plus GAS_WEBAPP_URL into .env
python app.py
```
Visit `http://localhost:5000` for the public site and `http://localhost:5000/admin/login` for the staff dashboard.
