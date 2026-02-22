from flask import Flask, request, jsonify, send_file, render_template
import os, zipfile, io, threading, time
from pathlib import Path

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 125 * 1024 * 1024  # 125MB total

# Allow frontend to call API (needed when serving from different port during dev)
@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Expose-Headers'] = 'X-Original-Size,X-New-Size,X-Saved-Percent'
    return response

PROCESSED_FOLDER = Path('processed')
PROCESSED_FOLDER.mkdir(exist_ok=True)

MAX_FILES = 5
MAX_FILE_MB = 25


def is_pdf(f):
    return f.filename.lower().endswith('.pdf')

def is_image(f):
    return f.filename.lower().split('.')[-1] in {'jpg','jpeg','png','webp','bmp'}

def file_too_big(f):
    f.seek(0, 2); size = f.tell(); f.seek(0)
    return size > MAX_FILE_MB * 1024 * 1024

def err(msg, code=400):
    return jsonify({'error': msg}), code


# ── MERGE ──────────────────────────────────────────────────────────
@app.route('/api/merge', methods=['POST'])
def merge():
    files = request.files.getlist('files')
    if len(files) < 2:
        return err('Upload at least 2 PDF files to merge')
    if len(files) > MAX_FILES:
        return err(f'Maximum {MAX_FILES} files allowed')
    for f in files:
        if not is_pdf(f): return err(f'"{f.filename}" is not a PDF')
        if file_too_big(f): return err(f'Each file must be under {MAX_FILE_MB}MB')
    try:
        from pypdf import PdfWriter, PdfReader
        writer = PdfWriter()
        for f in files:
            for page in PdfReader(io.BytesIO(f.read())).pages:
                writer.add_page(page)
        out = io.BytesIO()
        writer.write(out); out.seek(0)
        return send_file(out, as_attachment=True, download_name='merged.pdf', mimetype='application/pdf')
    except Exception as e:
        return err(f'Merge failed: {str(e)[:100]}')


# ── SPLIT ──────────────────────────────────────────────────────────
@app.route('/api/split', methods=['POST'])
def split():
    f = request.files.get('file')
    if not f: return err('No file uploaded')
    if not is_pdf(f): return err('Only PDF files allowed')
    if file_too_big(f): return err(f'File must be under {MAX_FILE_MB}MB')

    mode = request.form.get('mode', 'all')
    page_range = request.form.get('range', '').strip()

    try:
        from pypdf import PdfWriter, PdfReader
        reader = PdfReader(io.BytesIO(f.read()))
        total = len(reader.pages)

        if mode == 'all':
            pages = list(range(total))
        else:
            pages = []
            for part in page_range.split(','):
                part = part.strip()
                if '-' in part:
                    a, b = part.split('-')
                    pages += list(range(int(a)-1, min(int(b), total)))
                elif part.isdigit():
                    p = int(part)-1
                    if 0 <= p < total: pages.append(p)
        if not pages: return err('No valid pages in that range')

        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            for page_num in pages:
                w = PdfWriter()
                w.add_page(reader.pages[page_num])
                b = io.BytesIO(); w.write(b)
                zf.writestr(f'page_{page_num+1:03d}.pdf', b.getvalue())
        zip_buf.seek(0)
        return send_file(zip_buf, as_attachment=True, download_name='split_pages.zip', mimetype='application/zip')
    except Exception as e:
        return err(f'Split failed: {str(e)[:100]}')


# ── COMPRESS ───────────────────────────────────────────────────────
@app.route('/api/compress', methods=['POST'])
def compress():
    f = request.files.get('file')
    if not f: return err('No file uploaded')
    if not is_pdf(f): return err('Only PDF files allowed')
    if file_too_big(f): return err(f'File must be under {MAX_FILE_MB}MB')

    level = request.form.get('level', 'medium')
    # Support custom quality from slider (10-90), fallback to level presets
    try:
        quality = int(request.form.get('quality', 0))
        if not (10 <= quality <= 90):
            raise ValueError
    except (ValueError, TypeError):
        quality_map = {'low': 75, 'medium': 50, 'high': 20}
        quality = quality_map.get(level, 50)
    
    data = f.read()
    original_size = len(data)

    try:
        import pikepdf
        from PIL import Image
        pdf = pikepdf.open(io.BytesIO(data))

        for page in pdf.pages:
            try:
                xobjects = page.get('/Resources', {}).get('/XObject', {})
                for key in list(xobjects.keys()):
                    xobj = xobjects[key]
                    if xobj.get('/Subtype') == '/Image':
                        try:
                            img = Image.open(io.BytesIO(xobj.read_raw_bytes()))
                            if img.mode != 'RGB':
                                bg = Image.new('RGB', img.size, (255,255,255))
                                try: bg.paste(img, mask=img.split()[-1])
                                except: bg = img.convert('RGB')
                                img = bg
                            buf = io.BytesIO()
                            img.save(buf, 'JPEG', quality=quality, optimize=True)
                            xobj.stream_data = buf.getvalue()
                            xobj['/Filter'] = pikepdf.Name('/DCTDecode')
                            if '/DecodeParms' in xobj: del xobj['/DecodeParms']
                            xobj['/ColorSpace'] = pikepdf.Name('/DeviceRGB')
                        except: pass
            except: pass

        out = io.BytesIO()
        pdf.save(out, compress_streams=True,
                 stream_decode_level=pikepdf.StreamDecodeLevel.generalized,
                 object_stream_mode=pikepdf.ObjectStreamMode.generate,
                 normalize_content=True)
        out.seek(0)
        compressed = out.getvalue()
        new_size = len(compressed)
        saved = round((1 - new_size/original_size)*100, 1) if original_size else 0

        response = send_file(io.BytesIO(compressed), as_attachment=True,
                             download_name='compressed.pdf', mimetype='application/pdf')
        response.headers['X-Original-Size'] = str(original_size)
        response.headers['X-New-Size'] = str(new_size)
        response.headers['X-Saved-Percent'] = str(saved)
        return response
    except Exception as e:
        return err(f'Compression failed: {str(e)[:100]}')


# ── JPG → PDF ──────────────────────────────────────────────────────
@app.route('/api/jpg-to-pdf', methods=['POST'])
def jpg_to_pdf():
    files = request.files.getlist('files')
    if not files: return err('No images uploaded')
    if len(files) > MAX_FILES: return err(f'Maximum {MAX_FILES} images allowed')
    for f in files:
        if not is_image(f): return err(f'"{f.filename}" is not a supported image')
        if file_too_big(f): return err(f'Each file must be under {MAX_FILE_MB}MB')
    try:
        from PIL import Image
        from reportlab.pdfgen import canvas as rl_canvas
        from reportlab.lib.utils import ImageReader

        out = io.BytesIO()
        imgs = []
        for f in files:
            img = Image.open(io.BytesIO(f.read()))
            if img.mode != 'RGB':
                bg = Image.new('RGB', img.size, (255,255,255))
                try: bg.paste(img, mask=img.split()[-1])
                except: bg = img.convert('RGB')
                img = bg
            imgs.append(img)

        w, h = imgs[0].size
        c = rl_canvas.Canvas(out, pagesize=(w, h))
        for img in imgs:
            iw, ih = img.size
            c.setPageSize((iw, ih))
            buf = io.BytesIO()
            img.save(buf, 'JPEG', quality=92); buf.seek(0)
            c.drawImage(ImageReader(buf), 0, 0, iw, ih)
            c.showPage()
        c.save(); out.seek(0)
        return send_file(out, as_attachment=True, download_name='images.pdf', mimetype='application/pdf')
    except Exception as e:
        return err(f'Conversion failed: {str(e)[:100]}')


# ── PDF → JPG ──────────────────────────────────────────────────────
@app.route('/api/pdf-to-jpg', methods=['POST'])
def pdf_to_jpg():
    f = request.files.get('file')
    if not f: return err('No file uploaded')
    if not is_pdf(f): return err('Only PDF files allowed')
    if file_too_big(f): return err(f'File must be under {MAX_FILE_MB}MB')

    data = f.read()
    try:
        try:
            from pdf2image import convert_from_bytes
            images = convert_from_bytes(data, dpi=150, fmt='jpeg')
        except ImportError:
            import subprocess, tempfile
            with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
                tmp.write(data); tmp_path = tmp.name
            out_pat = tmp_path.replace('.pdf', '_%03d.jpg')
            subprocess.run(['gs','-dNOPAUSE','-dBATCH','-sDEVICE=jpeg','-r150',
                           f'-sOutputFile={out_pat}', tmp_path], capture_output=True, timeout=60)
            from PIL import Image
            images = []
            i = 1
            while True:
                p = out_pat % i
                if os.path.exists(p): images.append(Image.open(p)); i += 1
                else: break
            os.unlink(tmp_path)

        if not images:
            return err('Could not render this PDF. Please install poppler on the server.')

        from PIL import Image
        if len(images) == 1:
            buf = io.BytesIO()
            img = images[0]
            if img.mode != 'RGB': img = img.convert('RGB')
            img.save(buf, 'JPEG', quality=90); buf.seek(0)
            return send_file(buf, as_attachment=True, download_name='page_1.jpg', mimetype='image/jpeg')
        else:
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
                for i, img in enumerate(images):
                    buf = io.BytesIO()
                    if img.mode != 'RGB': img = img.convert('RGB')
                    img.save(buf, 'JPEG', quality=90)
                    zf.writestr(f'page_{i+1:03d}.jpg', buf.getvalue())
            zip_buf.seek(0)
            return send_file(zip_buf, as_attachment=True, download_name='pdf_pages.zip', mimetype='application/zip')
    except Exception as e:
        return err(f'PDF to JPG failed: {str(e)[:100]}')


@app.route('/')
def index():
    html_path = Path('index.html')
    if html_path.exists():
        return html_path.read_text()
    return '<h1>LoveYourPDF - index.html not found</h1>', 404

@app.route('/legal.html')
def legal():
    p = Path('legal.html')
    if p.exists():
        return p.read_text()
    return '<h1>Not found</h1>', 404

@app.route('/sitemap.xml')
def sitemap():
    p = Path('sitemap.xml')
    if p.exists():
        from flask import Response
        return Response(p.read_text(), mimetype='application/xml')
    return '<h1>Not found</h1>', 404

@app.route('/robots.txt')
def robots():
    return "User-agent: *\nAllow: /\nSitemap: https://www.loveyourpdf.com/sitemap.xml\n", 200, {'Content-Type': 'text/plain'}

@app.route('/health')
def health():
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    debug = os.environ.get('FLASK_ENV') == 'development'
    app.run(debug=debug, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
