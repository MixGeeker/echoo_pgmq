import importlib.util
from pathlib import Path
import subprocess
import tempfile
import json
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
import controller


class BudgetTests(unittest.TestCase):
    def test_commands_clamp_to_work_or_cleanup_deadline(self):
        with patch.object(controller.time,'monotonic',return_value=100): b=controller.Budget()
        with patch.object(controller.time,'monotonic',return_value=645), patch.object(controller.subprocess,'run') as run:
            run.return_value.stdout='okay'
            self.assertEqual(b.command(['echo','read'],timeout=20),'okay')
            self.assertEqual(run.call_args.kwargs['timeout'],5)
            b.command(['echo','normal cleanup'],timeout=20,cleanup=True)
            self.assertEqual(run.call_args.kwargs['timeout'],20)
        with patch.object(controller.time,'monotonic',return_value=650):
            with self.assertRaises(controller.BudgetExpired): b.command(['never-start'])

    def test_client_timeout_stops_only_owned_process_group(self):
        with patch.object(controller.time,'monotonic',return_value=100): b=controller.Budget()
        with patch.object(controller.time,'monotonic',return_value=101), patch.object(controller.subprocess,'Popen') as popen, patch.object(controller.os,'killpg') as kill:
            child=popen.return_value; child.pid=12345
            child.communicate.side_effect=[subprocess.TimeoutExpired('client',1),('','')]
            with self.assertRaises(subprocess.TimeoutExpired): b.client(['owned-client'],1)
            self.assertTrue(popen.call_args.kwargs['start_new_session'])
            kill.assert_called_once_with(12345,controller.signal.SIGTERM)

    def test_hard_stop_record_keeps_all_unrun_and_invalidates_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp); (output/'MANIFEST.json').write_text('old')
            state=controller.State(controller.schedule()); b=controller.Budget()
            controller.emergency_record(output,state,b)
            result=json.loads((output/'terminal-status.json').read_text())
            self.assertEqual(result['terminal_status'],'budget_insufficient')
            self.assertEqual(len(result['unrun_units']),10)
            self.assertFalse(result['manifest_complete'])
            self.assertFalse((output/'MANIFEST.json').exists())

    def test_main_g0_failure_does_not_call_ab(self):
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/'new'; calls=[]
            def command(b,argv,**kwargs):
                if argv[:3]==['docker','image','inspect']: return 'sha256:fixed'
                if argv[:2]==['docker','info']: return '{}'
                if argv[:2]==['git','rev-parse']: return 'fixed'
                return ''
            def unit(b,o,i,u,c):
                calls.append(u['label'])
                return {'gate':{'passed':False,'errors':['PG-only under 190 TPS']}}
            with patch.object(sys,'argv',['controller','--image','test','--output',str(output)]), patch.object(controller.os,'sched_getaffinity',return_value={0,1,2,3}), patch.object(controller.os,'sched_setaffinity'), patch.object(controller.Budget,'command',command), patch.object(controller,'run_unit',unit):
                self.assertEqual(controller.main(),0)
            self.assertEqual(calls,['G0'])
            result=json.loads((output/'terminal-status.json').read_text())
            self.assertEqual(result['terminal_status'],'environment_insufficient')
            self.assertEqual(len(result['unrun_units']),9)
            self.assertTrue((output/'MANIFEST.json').is_file())

if __name__=='__main__': unittest.main()
