"""Private conversation-scoped attachments and cooperation preferences."""
import base64
from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
import secrets
import sqlite3
import warnings
import zipfile
import xml.etree.ElementTree as ET

from .security import redact_text, sensitive_workspace_path
from .workspace import now

LEVELS = {'step', 'balanced', 'autonomous'}
DEFAULT_COOPERATION = {'planning': 'balanced', 'implementation': 'balanced'}
MAX_FILE = 5 * 1024 * 1024
MAX_FILES = 4
TEXT_SUFFIXES = {'.txt', '.md', '.csv', '.tsv', '.json', '.yaml', '.yml', '.py', '.r', '.js', '.ts', '.html', '.css', '.toml', '.log', '.xml', '.fasta', '.fa', '.vcf'}


def cooperation(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT_COOPERATION):
        raise ValueError('协作设置需要分别指定项目规划和项目实现。')
    if any(not isinstance(v, str) or v not in LEVELS for v in value.values()):
        raise ValueError('无效的协作方式。')
    return dict(value)


class ConversationAssets:
    def __init__(self, root):
        self.root=Path(root).resolve()
        self.path = Path(root) / '.pico/conversation-assets.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS attachments (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
                    mime TEXT NOT NULL, data BLOB NOT NULL, extracted TEXT NOT NULL,
                    sha256 TEXT NOT NULL, original_bytes INTEGER NOT NULL, created TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cooperation (owner TEXT PRIMARY KEY, settings TEXT NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        with sqlite3.connect(self.path, timeout=15) as db:
            db.row_factory = sqlite3.Row
            yield db

    def preferences(self, owner, value=None):
        with self.connect() as db:
            if value is not None:
                setting = cooperation(value)
                db.execute('INSERT OR REPLACE INTO cooperation VALUES (?,?)', (owner, json.dumps(setting)))
                return setting
            row = db.execute('SELECT settings FROM cooperation WHERE owner=?', (owner,)).fetchone()
        return json.loads(row[0]) if row else dict(DEFAULT_COOPERATION)

    @staticmethod
    def metadata(row):
        return {key: row[key] for key in ('id', 'name', 'mime', 'sha256', 'original_bytes', 'created')}

    def upload(self, owner, name, encoded):
        if (not isinstance(name, str) or not 1 <= len(name) <= 180 or
                '/' in name or '\\' in name or any(ord(c) < 32 for c in name) or sensitive_workspace_path(name)):
            raise ValueError('文件名无效或属于敏感配置文件。')
        if not isinstance(encoded, str) or len(encoded) > (MAX_FILE * 4 // 3 + 8):
            raise ValueError('单个附件不能超过5MB。')
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            raise ValueError('附件编码无效。') from None
        if not data or len(data) > MAX_FILE:
            raise ValueError('附件为空或超过5MB。')
        original_bytes = len(data)
        suffix = Path(name).suffix.lower()
        extracted = ''
        if suffix in {'.png', '.jpg', '.jpeg', '.webp'}:
            try:
                from PIL import Image, ImageOps
            except ImportError:
                raise ValueError('图片解析未安装，请安装 goaltree-agent[attachments]。') from None
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter('error', Image.DecompressionBombWarning)
                    with Image.open(io.BytesIO(data)) as image:
                        if image.format not in {'PNG', 'JPEG', 'WEBP'} or image.width * image.height > 16_000_000:
                            raise ValueError('图片格式无效或像素数过大。')
                        image = ImageOps.exif_transpose(image)
                        image.thumbnail((768, 768))
                        # Strip metadata and re-encode. Alpha is composited on white.
                        background = Image.new('RGB', image.size, 'white')
                        if 'A' in image.getbands():
                            background.paste(image, mask=image.getchannel('A'))
                        else:
                            background.paste(image.convert('RGB'))
                        output = io.BytesIO()
                        background.save(output, format='JPEG', quality=75, optimize=True)
                        data = output.getvalue()
            except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
                raise ValueError('图片无法解码或超过像素限制。') from None
            mime = 'image/jpeg'
        elif suffix == '.docx':
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    info = archive.getinfo('word/document.xml')
                    if info.file_size > 2_000_000 or sum(i.file_size for i in archive.infolist()) > 10_000_000:
                        raise ValueError('Word文档解压内容过大。')
                    document = ET.fromstring(archive.read(info))
                    ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
                    extracted = '\n'.join(''.join(p.itertext()) for p in document.findall('.//w:p', ns))
                    if len(extracted) > 60000 or not extracted.strip():
                        raise ValueError('Word文档没有可提取文字或超过60000字。')
            except (zipfile.BadZipFile, KeyError, ET.ParseError):
                raise ValueError('不是有效DOCX文档。') from None
            mime = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        elif suffix == '.pdf':
            if not data.startswith(b'%PDF-'):
                raise ValueError('不是有效PDF。')
            try:
                from pypdf import PdfReader
            except ImportError:
                raise ValueError('PDF解析未安装，请安装 goaltree-agent[attachments]。') from None
            try:
                reader = PdfReader(io.BytesIO(data))
                if reader.is_encrypted or len(reader.pages) > 40:
                    raise ValueError('PDF不能加密，且最多40页。')
                chunks, size = [], 0
                for page in reader.pages:
                    chunk = page.extract_text() or ''
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > 60000:
                        raise ValueError('PDF文字超过60000字，请拆分后上传。')
                extracted = '\n\n'.join(chunks)
                if not extracted.strip():
                    raise ValueError('PDF没有可提取文字，扫描件请作为图片上传。')
            except ValueError:
                raise
            except Exception:
                raise ValueError('PDF解析失败。') from None
            mime = 'application/pdf'
        elif suffix in TEXT_SUFFIXES:
            try:
                extracted = data.decode('utf-8-sig')
            except UnicodeDecodeError:
                raise ValueError('文本文件需要UTF-8编码。') from None
            if '\0' in extracted or len(extracted) > 60000:
                raise ValueError('文本包含二进制内容或超过60000字。')
            mime = 'text/plain'
        else:
            raise ValueError('暂支持UTF-8文本/代码、PDF、DOCX、PNG、JPEG与WebP图片。')
        row = {'id': 'att_' + secrets.token_hex(12), 'owner': owner, 'name': name, 'mime': mime,
               'data': data, 'extracted': redact_text(extracted), 'sha256': hashlib.sha256(data).hexdigest(),
               'original_bytes': original_bytes, 'created': now()}
        from .privacy import PrivacyStore,inspect_text
        privacy=PrivacyStore(self.root)
        if privacy.enabled:
            level,_=inspect_text(extracted)
            privacy.label('artifact:'+row['id'],'sensitive' if mime.startswith('image/') else level)
        with self.connect() as db:
            # Bounded private storage; no silent eviction of older attachments.
            db.execute('BEGIN IMMEDIATE')
            usage = db.execute('SELECT COALESCE(SUM(original_bytes),0),COUNT(*) FROM attachments').fetchone()
            if usage[0] + original_bytes > 64 * 1024 * 1024 or usage[1] >= 100:
                raise ValueError('工作区附件存储达到上限（64MB/100个），请整理后重试。')
            db.execute('INSERT INTO attachments VALUES (:id,:owner,:name,:mime,:data,:extracted,:sha256,:original_bytes,:created)', row)
        return self.metadata(row)

    def get(self, owner, ids):
        if not isinstance(ids, list) or len(ids) > MAX_FILES or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
            raise ValueError('每条消息最多4个不同附件。')
        rows = []
        with self.connect() as db:
            for id in ids:
                row = db.execute('SELECT * FROM attachments WHERE id=? AND owner=?', (id, owner)).fetchone()
                if row is None:
                    raise ValueError('附件不存在或不属于当前对话。')
                rows.append(dict(row))
        return rows


def attachment_context(rows):
    return [{'name': r['name'], 'sha256': r['sha256'], 'mime': r['mime'],
             'text': r['extracted'] if not r['mime'].startswith('image/') else '[图片通过原生多模态输入提供]'} for r in rows]
