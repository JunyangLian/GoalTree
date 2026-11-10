"""Trusted standalone PDF text parser. Stdin bytes only; no files, JS, OCR or network calls."""
import io
import json
import os
import sys

MAX_BYTES = 5 * 1024 * 1024
MAX_PAGES = 40
MAX_TEXT = 60000
MEMORY = 512 * 1024 * 1024
_job = None


def resource_limit():
    global _job
    if os.name!='nt':
        import resource
        resource.setrlimit(resource.RLIMIT_AS,(MEMORY,MEMORY))
        return
    import ctypes as C
    from ctypes import wintypes as W
    class Basic(C.Structure):
        _fields_=[('process_time',C.c_longlong),('job_time',C.c_longlong),('flags',W.DWORD),
          ('min_working',C.c_size_t),('max_working',C.c_size_t),('active_processes',W.DWORD),
          ('affinity',C.c_size_t),('priority',W.DWORD),('scheduling',W.DWORD)]
    class IO(C.Structure):_fields_=[(n,C.c_ulonglong) for n in ('r','w','o','rb','wb','ob')]
    class Extended(C.Structure):
        _fields_=[('basic',Basic),('io',IO),('process_memory',C.c_size_t),('job_memory',C.c_size_t),('peak_process',C.c_size_t),('peak_job',C.c_size_t)]
    k=C.WinDLL('kernel32',use_last_error=True)
    k.CreateJobObjectW.argtypes=[C.c_void_p,W.LPCWSTR];k.CreateJobObjectW.restype=W.HANDLE
    k.SetInformationJobObject.argtypes=[W.HANDLE,C.c_int,C.c_void_p,W.DWORD];k.SetInformationJobObject.restype=W.BOOL
    k.AssignProcessToJobObject.argtypes=[W.HANDLE,W.HANDLE];k.AssignProcessToJobObject.restype=W.BOOL
    k.GetCurrentProcess.argtypes=[];k.GetCurrentProcess.restype=W.HANDLE
    _job=k.CreateJobObjectW(None,None);limits=Extended()
    limits.basic.flags=0x100|0x8;limits.basic.active_processes=1;limits.process_memory=MEMORY
    if not _job or not k.SetInformationJobObject(_job,9,C.byref(limits),C.sizeof(limits)) or not k.AssignProcessToJobObject(_job,k.GetCurrentProcess()):
        raise ValueError('PDF parser resource limits unavailable; refusing unbounded parsing')


def parse(data):
    if not data.startswith(b'%PDF-') or len(data)>MAX_BYTES:raise ValueError('Invalid PDF signature or exceeds 5 MiB')
    import pypdf
    import pypdf.filters as filters
    if not hasattr(filters,'ZLIB_MAX_OUTPUT_LENGTH'):raise ValueError('PDF parser needs pypdf>=6.21.1')
    # Bound compressed streams as well as the process; do not trust declared sizes.
    for key in ('ZLIB_MAX_OUTPUT_LENGTH','LZW_MAX_OUTPUT_LENGTH','RUN_LENGTH_MAX_OUTPUT_LENGTH',
                'MAX_ARRAY_BASED_STREAM_OUTPUT_LENGTH','MAX_DECLARED_STREAM_LENGTH'):
        if hasattr(filters,key):setattr(filters,key,8*1024*1024)
    reader=pypdf.PdfReader(io.BytesIO(data),strict=False)
    if reader.is_encrypted:raise ValueError('Encrypted PDF is not supported; no password attempts')
    count=len(reader.pages)
    if not 1<=count<=MAX_PAGES:raise ValueError('PDF must have 1..40 pages')
    texts=[];size=0;stream_size=0
    for page in reader.pages:
        contents=page.get_contents()
        amount=len(contents.get_data()) if contents else 0;stream_size+=amount
        if amount>8*1024*1024 or stream_size>32*1024*1024:raise ValueError('PDF decoded content stream limit exceeded')
        text=page.extract_text() or '';size+=len(text)
        if size>MAX_TEXT:raise ValueError('PDF exceeds 60000 extracted characters; split the document')
        texts.append(text)
    title=str((reader.metadata or {}).get('/Title',''))[:160]
    return {'title':title,'pages':texts,'page_count':count,'parser':'pypdf/'+pypdf.__version__,
            'memory_limit_bytes':MEMORY,'text_characters':size}


if __name__=='__main__':
    try:
        resource_limit()
        result={'ok':True,**parse(sys.stdin.buffer.read(MAX_BYTES+1))}
    except ImportError:result={'ok':False,'error':'PDF dependency missing: install goaltree-agent[web-pdf]'}
    except ValueError as exc:result={'ok':False,'error':str(exc)[:300]}
    except Exception:result={'ok':False,'error':'PDF parsing failed or resource limit reached; no content accepted'}
    sys.stdout.buffer.write(json.dumps(result,ensure_ascii=False).encode('utf-8'))
