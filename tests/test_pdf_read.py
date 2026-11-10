"""Synthetic PDFs, actual bounded parser processes, transport and citation regressions."""
import io
import json
import subprocess
import time
from unittest.mock import patch
from urllib.parse import quote

import pytest

from pico.pdf_read import PDFError,document_url,extract_pdf
from pico.web_tools import PublicWeb,MAX_PDF_BYTES,WebAccessError
from test_web_context import agent
from test_web_access import Response,transport as http_transport


def pdf(pages,encrypted=False):
    pypdf=pytest.importorskip('pypdf')
    from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
    writer=pypdf.PdfWriter()
    for text in pages:
        page=writer.add_blank_page(width=600,height=800)
        if text:
            font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
            page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
            escaped=[line.replace('\\','\\\\').replace('(','\\(').replace(')','\\)') for line in text.splitlines()]
            stream=DecodedStreamObject();stream.set_data(('BT /F1 12 Tf 12 TL 40 700 Td '+' T* '.join('('+s+') Tj' for s in escaped)+' ET').encode('ascii'))
            page[NameObject('/Contents')]=writer._add_object(stream)
    writer.add_metadata({'/Title':'Synthetic document'})
    if encrypted:writer.encrypt('fixture-password')
    out=io.BytesIO();writer.write(out);return out.getvalue()


def online(a,monkeypatch,data,url='https://example.org/notice.pdf'):
    import hashlib
    calls=[]
    def get(target,accepted,**kwargs):
        a.web_context.client.on_request();calls.append(target)
        assert 'application/pdf' in accepted
        return {'url':url,'content_type':'application/pdf','sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data),'retrieved_at':'fixture'},data
    monkeypatch.setattr(a.web_context.client,'get',get);return calls


def test_real_worker_extracts_text_under_resource_limits():
    result=extract_pdf(pdf(['Official date November 6-8 2026','Second physical page']),lambda:None,time.monotonic()+5)
    assert result['page_count']==2 and 'November 6-8' in result['pages'][0]
    assert 'Second physical page' in result['pages'][1] and result['memory_limit_bytes']==512*1024*1024


@pytest.mark.parametrize('data,reason',[
    (lambda:pdf(['secret'],encrypted=True),'Encrypted'),
    (lambda:pdf(['']*41),'40 pages'),
    (lambda:b'%PDF-1.7\ncorrupt\n%%EOF','parsing failed'),
    (lambda:pdf(['X'*60001]),'60000'),
])
def test_unsupported_pdf_fails_without_accepting_partial_content(data,reason):
    with pytest.raises(PDFError,match=reason):extract_pdf(data(),lambda:None,time.monotonic()+5)


def test_transport_accepts_only_pdf_binary_with_separate_size_limit(monkeypatch):
    body=b'%PDF-1.7\n'+b'x'*(600*1024)
    calls=http_transport(monkeypatch,[Response(headers={'Content-Type':'application/pdf'},body=body)])
    meta,raw=PublicWeb().get('https://example.org/doc',{'application/pdf'})
    assert raw==body and isinstance(raw,bytes) and meta['bytes']>512*1024 and calls[0]['connection'].closed
    for response,reason in [
        (Response(headers={'Content-Type':'application/pdf'},body=b'<html>not a pdf</html>'),'signature'),
        (Response(headers={'Content-Type':'application/pdf','Content-Length':str(MAX_PDF_BYTES+1)}),'5 MiB'),
        (Response(headers={'Content-Type':'application/pdf'},body=b'%PDF-'+b'x'*MAX_PDF_BYTES),'5 MiB'),
        (Response(headers={'Content-Type':'application/octet-stream'},body=b'%PDF-1.7'),'content type'),
    ]:
        http_transport(monkeypatch,[response])
        with pytest.raises(WebAccessError,match=reason):PublicWeb().get('https://example.org/doc',{'application/pdf'})


def test_pages_find_cache_and_exact_citations_without_repeat_download(tmp_path,monkeypatch):
    a=agent(tmp_path);calls=online(a,monkeypatch,pdf(['Opening\nMeeting date November 6-8 2026\nLEFT','RIGHT\nSecond page answer']))
    first=json.loads(a.web_context.read(url='https://example.org/notice.pdf',limit=1))
    assert first['document_type']=='pdf' and first['page_count']==2 and first['lines'][0]['page']==1
    assert first['citation'].endswith('P1-P1:L1-L1') and first['truncated']
    found=json.loads(a.web_context.find(first['page_id'],'Second page answer'))
    match=found['matches'][0];assert match['page']==2 and ':P2:' in match['citation']
    assert json.loads(a.web_context.find(first['page_id'],'LEFT RIGHT'))['total_matches']==0
    selected=json.loads(a.web_context.read(page_id=first['page_id'],pdf_page=2))
    assert all(row['page']==2 for row in selected['lines']) and selected['start_page']==selected['end_page']==2
    assert selected['lines'][-1]['text']=='Second page answer' and len(calls)==1
    cached=json.loads(a.web_context.read(url='https://example.org/notice.pdf'))
    assert cached['cache']=='hit' and len(calls)==1
    with pytest.raises(WebAccessError,match='page count'):a.web_context.read(page_id=first['page_id'],pdf_page=3)
    with pytest.raises(ValueError):agent(tmp_path,'other').web_context.read(page_id=first['page_id'])


def test_scan_and_partial_text_are_not_claimed_fully_read(tmp_path,monkeypatch):
    a=agent(tmp_path);online(a,monkeypatch,pdf(['','']))
    result=a.execute_tool('web_read',{'url':'https://example.org/scan.pdf'})
    data=json.loads(result.content.split('\nEvidence ID:')[0])
    assert data['text_extraction_status']=='needs_ocr' and data['empty_text_pages']==[1,2]
    assert result.metadata['tool_status']=='partial_success' and result.metadata['tool_error_code']=='pdf_text_incomplete'
    assert json.loads(a.web_context.find(data['page_id'],'no extractable'))['total_matches']==0
    b=agent(tmp_path,'mixed');online(b,monkeypatch,pdf(['Actual text','']))
    mixed=json.loads(b.web_context.read(url='https://example.org/mixed.pdf'))
    assert mixed['text_extraction_status']=='partial_text' and mixed['empty_text_pages']==[2]


def test_output_trimming_keeps_pdf_page_and_line_citations_exact(tmp_path,monkeypatch):
    a=agent(tmp_path);online(a,monkeypatch,pdf(['first\nsecond','\n'.join('Long text '+('data '*30) for _ in range(30))]),url='https://example.org/'+('a'*900)+'.pdf')
    result=json.loads(a.web_context.read(url='https://example.org/notice.pdf',limit=20))
    assert result['end_line']==result['lines'][-1]['line']
    assert result['end_page']==result['lines'][-1]['page'] and result['start_page']==result['lines'][0]['page']
    assert result['citation'].endswith(f"P{result['start_page']}-P{result['end_page']}:L{result['start_line']}-L{result['end_line']}")
    assert len(json.dumps(result,ensure_ascii=False))<=3800


def test_viewer_is_resolved_without_js_and_decoded_secrets_do_not_egress(tmp_path,monkeypatch):
    a=agent(tmp_path);calls=online(a,monkeypatch,pdf(['Notice']))
    viewer='https://example.org/pdfjs/web/viewer.html?file=%2Fnotice.pdf'
    data=json.loads(a.web_context.read(url=viewer))
    assert calls==['https://example.org/notice.pdf']
    assert json.loads(a.web_context.read(url='https://example.org/notice.pdf'))['page_id']==data['page_id'] and len(calls)==1
    for bad in ['https://example.org/pdfjs/web/viewer.html?file=file:///C:/secret.pdf',
                'https://example.org/pdfjs/web/viewer.html?file=http://example.org/doc.pdf']:
        with pytest.raises(WebAccessError):document_url(bad)
    monkeypatch.setenv('BIGMODEL_API_KEY','private-fixture-key-for-pdf')
    encoded=''.join('%'+format(ord(c),'02x') for c in 'private-fixture-key-for-pdf')
    with pytest.raises(WebAccessError,match='sensitive'):
        a.web_context.read(url='https://example.org/notice.pdf?key='+encoded)
    assert len(calls)==1


def test_parser_timeout_cancellation_and_environment_are_bounded(monkeypatch):
    monkeypatch.setenv('BIGMODEL_API_KEY','never-pass-to-worker')
    processes=[]
    class Hung:
        returncode=None
        def __init__(self,cmd,**kwargs):
            self.kwargs=kwargs;self.killed=False;processes.append(self)
            assert '-I' in cmd and not kwargs.get('shell') and 'BIGMODEL_API_KEY' not in kwargs['env']
        def communicate(self,**kwargs):
            if self.killed:return b'',None
            raise subprocess.TimeoutExpired('fixture',.1)
        def poll(self):return 0 if self.killed else None
        def kill(self):self.killed=True
    monkeypatch.setattr('pico.pdf_read.subprocess.Popen',Hung)
    with pytest.raises(PDFError,match='timeout'):extract_pdf(b'%PDF-fixture',lambda:None,time.monotonic()+.05)
    assert processes[0].killed
    checks=[0]
    def cancelled():
        checks[0]+=1
        if checks[0]>1:raise RuntimeError('cancel fixture')
    with pytest.raises(RuntimeError,match='cancel fixture'):extract_pdf(b'%PDF-fixture',cancelled,time.monotonic()+1)
    assert processes[-1].killed
