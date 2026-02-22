# LoveYourPDF — Backend

## Local Development
```bash
pip install -r requirements.txt
python app.py
```
Visit: http://localhost:5000

## Deploy to Railway
1. Push to GitHub
2. Connect repo on railway.app
3. Set env var: PORT=5000
4. Deploy — done!

## API Endpoints
- POST /api/merge       — files[] (pdf)
- POST /api/split       — file (pdf), mode (all/range), range (string)
- POST /api/compress    — file (pdf), level (low/medium/high), quality (10-90)
- POST /api/jpg-to-pdf  — files[] (jpg/png/webp/bmp)
- POST /api/pdf-to-jpg  — file (pdf)

## System Requirements (server)
- Python 3.10+
- poppler-utils (for pdf2image): apt install poppler-utils
