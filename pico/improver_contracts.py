"""Frozen host-owned external unittest contracts. Not sent in the Improver packet."""
import json
from pathlib import Path
import tempfile
import unittest
import uuid


class ImprovementContracts(unittest.TestCase):
    def test_feedback_source_minimized(self):
        from pico.providers.clients import FakeModelClient
        from pico.runtime import Pico, SessionStore
        from pico.workspace import WorkspaceContext
        with tempfile.TemporaryDirectory(dir='.') as folder:
            root=Path(folder).resolve()
            agent=Pico(FakeModelClient(['<final>收到。</final>']),WorkspaceContext.build(root),
                SessionStore(root/'.pico/sessions'),harness_profile='workbench',max_depth=0)
            marker='private-research-'+uuid.uuid4().hex
            agent.ask('这份材料需要调整，'+marker+' 是此次临时说明。')
            sources=agent.capabilities.learning.store.sources(session_id=agent.session['id'])
            self.assertTrue(sources)
            self.assertNotIn(marker,json.dumps(sources))

    def test_opt_out_and_presentation_preference_preserved(self):
        from pico.providers.clients import FakeModelClient
        from pico.runtime import Pico, SessionStore
        from pico.workspace import WorkspaceContext
        with tempfile.TemporaryDirectory(dir='.') as folder:
            root=Path(folder).resolve()
            agent=Pico(FakeModelClient(['<final>收到。</final>','<final>收到。</final>']),WorkspaceContext.build(root),
                SessionStore(root/'.pico/sessions'),harness_profile='workbench',max_depth=0)
            agent.ask('请记住，报告先给简短提纲。')
            self.assertTrue(agent.capabilities.learning.store.list(session_id=agent.session['id']))
            agent.ask('不要记住本条内容。')
            self.assertFalse(any(s['run_id']==agent.current_task_state.run_id for s in
                agent.capabilities.learning.store.sources(session_id=agent.session['id']) if s['category']=='feedback'))

    def test_permission_boundary_unchanged(self):
        from pico.providers.clients import FakeModelClient
        from pico.runtime import Pico, SessionStore
        from pico.workspace import WorkspaceContext
        with tempfile.TemporaryDirectory(dir='.') as folder:
            root=Path(folder).resolve()
            agent=Pico(FakeModelClient([]),WorkspaceContext.build(root),SessionStore(root/'.pico/sessions'),
                harness_profile='workbench',approval_policy='auto',max_depth=0)
            result=agent.execute_tool('read_file',{'path':'.pico/internal.sqlite3'})
            self.assertEqual(result.metadata['tool_status'],'rejected')


if __name__=='__main__':unittest.main()
