"""Host-selected folders with request-pinned, independently persisted workbenches."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
from contextlib import contextmanager


def workspace_id(path):
    return 'ws_' + hashlib.sha256(os.path.normcase(str(Path(path).resolve())).encode()).hexdigest()[:20]


class WorkspaceManager:
    def __init__(self, initial, registry=None, factory=None, folder_picker=None):
        self.initial=initial
        self.path=Path(registry) if registry else initial.root/'.pico/workspaces.sqlite3'
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock()
        self.apps={workspace_id(initial.root):initial}
        self.default_id=workspace_id(initial.root)
        self.factory=factory or self._build
        self.folder_picker=folder_picker or choose_folder
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS folders(id TEXT PRIMARY KEY,path TEXT UNIQUE NOT NULL)')
            db.execute('INSERT OR IGNORE INTO folders VALUES (?,?)',(self.default_id,str(initial.root)))

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=10)
        try:
            with db:yield db
        finally:db.close()

    def _build(self, root):
        from .web import Workbench
        original=self.initial
        return Workbench(root, verification_command=original.verification_command,
            model_timeout=original.args.openai_timeout,max_new_tokens=original.max_new_tokens,
            shell_backend=original.shell_backend,native_input_budget=original.native_context_policy.input_budget,
            native_context_window=original.native_context_policy.context_window,harness_profile=original.harness_profile,
            embedding_mode=original.embedding_mode,model_factory=original.make_model,
            network_route=original.network_route,load_env=False,web_access=original.web_access,
            search_backend=original.search_backend,search_mcp=original.search_mcp,
            web_output_chars=original.web_budget_options['output_chars'],
            web_result_chars=original.web_budget_options['result_chars'],
            web_read_lines=original.web_budget_options['read_lines'])

    def folders(self):
        with self.connect() as db:rows=db.execute('SELECT id,path FROM folders ORDER BY rowid').fetchall()
        return [{'id':id,'path':path,'name':Path(path).name or path,'available':Path(path).is_dir()} for id,path in rows]

    def get(self,id=None):
        id=id or self.default_id
        with self.lock:
            if id not in self.apps:
                with self.connect() as db:row=db.execute('SELECT path FROM folders WHERE id=?',(id,)).fetchone()
                if row is None:raise ValueError('工作区未登记，请从左侧打开文件夹。')
                root=Path(row[0])
                if not root.is_dir():raise ValueError('工作区目录已移动或不可访问。')
                self.apps[id]=self.factory(root)
            return self.apps[id]

    def open(self,path):
        if not isinstance(path,str) or not path.strip() or len(path)>2000:
            raise ValueError('请提供有效文件夹路径。')
        root=Path(path).expanduser()
        if not root.is_absolute():raise ValueError('工作区需要绝对路径。')
        root=root.resolve(strict=True)
        if not root.is_dir():raise ValueError('请选择文件夹，不能选择文件。')
        id=workspace_id(root)
        with self.lock:
            if id not in self.apps:
                registered=any(folder['id']==id for folder in self.folders())
                if not registered and len(self.folders())>=20:raise ValueError('最多保存20个工作区。')
                app=self.factory(root)  # Fail before registry commit; no ACL relaxation.
                with self.connect() as db:db.execute('INSERT OR IGNORE INTO folders VALUES (?,?)',(id,str(root)))
                self.apps[id]=app
        return id

    def config(self,app):
        with self.lock:
            return {'workspace_id':workspace_id(app.root),'workspaces':self.folders(),'workspace_switching':True,
                    'global_active_workspace':next((id for id,a in self.apps.items() if a.active_id),None)}

    def submit(self,app,data):
        with self.lock:
            if any(a.active_id for a in self.apps.values()):
                from .web import RequestError
                raise RequestError('已有工作区正在运行，请等待结束或先停止该运行。',409)
            return app.submit(data)

    def pick(self,current):
        path=self.folder_picker(str(current.root))
        return self.open(path) if path else None


def choose_folder(initial):
    """Run the OS directory dialog in its own GUI process, not a server thread."""
    import subprocess,sys
    environment={k:v for k,v in os.environ.items() if k.upper() in {'PATH','SYSTEMROOT','WINDIR','TEMP','TMP','USERPROFILE','HOMEDRIVE','HOMEPATH','DISPLAY','LANG'}}
    environment['PYTHONIOENCODING']='utf-8'
    try:
        result=subprocess.run([sys.executable,str(Path(__file__).with_name('folder_picker.py')),initial],
            env=environment,capture_output=True,text=True,encoding='utf-8',timeout=300,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    except subprocess.TimeoutExpired:
        raise ValueError('文件夹选择超时；可以直接粘贴绝对路径。') from None
    if result.returncode:raise ValueError('系统文件夹选择器不可用，请在窗口中粘贴文件夹绝对路径。')
    try:return json.loads(result.stdout)['path']
    except (ValueError,KeyError):raise ValueError('文件夹选择器未返回有效结果。') from None
