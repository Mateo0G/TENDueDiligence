# Both Railway services (web and worker) build from this same image --
# each overrides CMD with its own Start Command in the Railway dashboard
# (uvicorn app.main:app ... / python -m app.worker). LibreOffice is here
# for docx->PDF conversion (see app/pdf_convert.py): python-docx can build
# a .docx but has no rendering engine of its own, and LibreOffice headless
# is the reliable open-source way to turn that into a PDF without an
# external conversion service.
FROM python:3.13-slim

# libreoffice-writer pulls in libreoffice-core automatically -- avoids the
# full libreoffice metapackage (Impress/Calc/Base/etc.) this app never
# touches. fonts-dejavu-core covers common document fonts so converted
# PDFs don't fall back to a missing-glyph box for ordinary text.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-writer \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
