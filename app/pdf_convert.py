"""docx -> PDF conversion via LibreOffice headless (see the repo's
Dockerfile -- this is why the deployed image isn't Railpack's slim
auto-detected Python build). python-docx can build a .docx but has no
layout/rendering engine of its own; `soffice --headless --convert-to pdf`
is the reliable open-source way to get a real PDF out of one without
standing up a separate conversion service.
"""
import os
import subprocess
import tempfile
import uuid


class PdfConversionError(RuntimeError):
    pass


def derive_pdf_key(docx_object_key: str) -> str:
    """The final report's PDF is stored alongside its docx under the same
    object key with the extension swapped -- shared by app/main.py (to
    look it up) and app/tasks/final_render.py (to write it), so the two
    can never derive different keys for the same docx."""
    if docx_object_key.endswith(".docx"):
        return docx_object_key[: -len(".docx")] + ".pdf"
    return docx_object_key + ".pdf"


def convert_docx_to_pdf(docx_bytes: bytes, timeout: int = 120) -> bytes:
    """Shells out to a headless soffice per call rather than keeping one
    running -- Stage 6 is one task per job, but the worker runs several
    tasks concurrently (see app/worker.py's WORKER_CONCURRENCY) and the
    web service can get concurrent requests too, so two conversions can
    genuinely overlap. Each gets its own -env:UserInstallation profile
    dir so they don't contend over LibreOffice's user-profile lock file
    (the default shared profile serializes/fails under concurrent use).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        docx_path = os.path.join(tmpdir, "input.docx")
        with open(docx_path, "wb") as f:
            f.write(docx_bytes)

        profile_dir = os.path.join(tmpdir, "lo_profile")
        result = subprocess.run(
            [
                "soffice",
                f"-env:UserInstallation=file://{profile_dir}",
                "--headless",
                "--norestore",
                "--convert-to", "pdf",
                "--outdir", tmpdir,
                docx_path,
            ],
            capture_output=True,
            timeout=timeout,
        )
        pdf_path = os.path.join(tmpdir, "input.pdf")
        if result.returncode != 0 or not os.path.exists(pdf_path):
            raise PdfConversionError(
                f"soffice conversion failed (exit {result.returncode}): "
                f"{result.stderr.decode(errors='replace')[-2000:]}"
            )
        with open(pdf_path, "rb") as f:
            return f.read()
