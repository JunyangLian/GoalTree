"""Bounded PDF worker orchestration and safe PDF.js link resolution."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import parse_qs, urljoin, urlsplit

from .web_tools import MAX_PDF_BYTES, WebAccessError, public_url


class PDFError(WebAccessError):
    pdf_error = True


def document_url(url):
    parts=urlsplit(url)
    if '/pdfjs/' not in parts.path.lower() or not parts.path.lower().endswith('/viewer.html'):return url
    files=parse_qs(parts.query).get('file',[])
    if len(files)!=1:raise PDFError('PDF.js viewer must contain one file URL')
    target=public_url(urljoin(url,files[0]))
    if not urlsplit(target).path.lower().endswith('.pdf'):raise PDFError('PDF.js file must identify a PDF document')
    if parts.scheme=='https' and urlsplit(target).scheme!='https':raise PDFError('HTTPS PDF viewer downgrade is blocked')
    return target


def extract_pdf(data,check,deadline):
    if not isinstance(data,bytes) or not data.startswith(b'%PDF-') or len(data)>MAX_PDF_BYTES:
        raise PDFError('Invalid PDF signature or exceeds 5 MiB')
    check();deadline=min(deadline,time.monotonic()+5)
    if deadline<=time.monotonic():raise PDFError('PDF parsing deadline exceeded')
    environment={k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','WINDIR','TEMP','TMP'}}
    command=[sys.executable,'-I','-B',str(Path(__file__).with_name('pdf_worker.py').resolve())]
    process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
        cwd=str(Path(sys.executable).parent),env=environment,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    try:
        payload=data
        while True:
            check();remaining=deadline-time.monotonic()
            if remaining<=0:raise PDFError('PDF parsing timeout; worker terminated')
            try:
                output,_=process.communicate(input=payload,timeout=min(.1,remaining));break
            except subprocess.TimeoutExpired:payload=None
        if process.returncode or len(output)>512*1024:raise PDFError('PDF worker failed or exceeded output limit')
        try:result=json.loads(output)
        except (ValueError,UnicodeError):raise PDFError('PDF worker returned invalid output') from None
        if not isinstance(result,dict) or not result.get('ok'):raise PDFError(result.get('error','PDF parse failed') if isinstance(result,dict) else 'PDF parse failed')
        if (not isinstance(result.get('pages'),list) or not 1<=len(result['pages'])<=40
                or any(not isinstance(p,str) for p in result['pages']) or sum(map(len,result['pages']))>60000):
            raise PDFError('PDF worker returned invalid page text')
        return result
    finally:
        if process.poll() is None:process.kill()
        process.communicate(timeout=3)
