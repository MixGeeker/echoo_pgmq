import importlib.util
from pathlib import Path
import unittest
spec = importlib.util.spec_from_file_location('policy', Path(__file__).with_name('policy.py'))
p = importlib.util.module_from_spec(spec); spec.loader.exec_module(p)


def rows(rate=200, latency=1):
    return [{'start_ns':int(i*1e9/rate), 'complete_ns':int(i*1e9/rate+latency*1e6), 'latency_ms':latency} for i in range(int(rate*20))]


class PolicyTests(unittest.TestCase):
    def test_schedule_predeclared(self):
        self.assertEqual(p.schedule(), p.schedule())
        self.assertEqual(set(p.schedule()['orders']), {'AB','BA'})
        self.assertEqual(len(p.schedule()['units']),10)
        self.assertEqual(p.schedule()['timed_load_seconds'],310)

    def test_gate_requires_both_subwindows(self):
        bad = rows()
        for row in bad[:101]: row['complete_ns'] += 10_000_000_000
        self.assertFalse(p.gate_result(bad,0)['passed'])
        self.assertTrue(p.gate_result(rows(),0)['passed'])
        self.assertFalse(p.gate_result(rows(),0,['sampler failure'])['passed'])

    def test_threshold_and_drift(self):
        self.assertTrue(p.gate_result(rows(190),0)['passed'])
        self.assertFalse(p.gate_result(rows(189),0)['passed'])
        a = p.gate_result(rows(),0)
        b = p.gate_result(rows(latency=3),0)
        self.assertFalse(p.drift_result(a,b)['passed'])

    def test_g0_fail_fast_and_unrun_record(self):
        plan=p.schedule(); state=p.State(plan)
        state.add(plan['units'][0],{'gate':p.gate_result(rows(170),0)})
        self.assertEqual(state.status,'environment_insufficient')
        self.assertEqual(len(state.report()['unrun_units']),9)
        with self.assertRaises(RuntimeError): state.add(plan['units'][1],{})

    def test_later_gate_invalidates_and_stops(self):
        plan=p.schedule(); state=p.State(plan)
        state.add(plan['units'][0],{'gate':p.gate_result(rows(),0)})
        for unit in plan['units'][1:3]: state.add(unit,{'status':'passed','trace_complete':True,'fixed_load_eligible':True})
        state.add(plan['units'][3],{'gate':p.gate_result(rows(latency=3),0)})
        self.assertEqual(state.pairs['1']['status'],'invalid_gate_or_drift')
        self.assertEqual(len(state.report()['unrun_units']),6)

    def test_budget_stop_leaves_unit_unrun(self):
        plan=p.schedule(); state=p.State(plan)
        self.assertFalse(p.admit_unit(plan['units'][1],500)['admitted'])
        self.assertTrue(p.admit_unit(plan['units'][1],400)['admitted'])
        state.stop('budget_insufficient','stage admission refused')
        self.assertEqual(len(state.report()['unrun_units']),10)

    def test_missing_trace_and_under_load(self):
        plan=p.schedule(); state=p.State(plan)
        for unit in plan['units']:
            result={'gate':p.gate_result(rows(),0)} if unit['kind']=='gate' else {'status':'passed','trace_complete':True,'fixed_load_eligible':False}
            state.add(unit,result)
        self.assertEqual(state.status,'completed_diagnostic_only')
        state=p.State(plan); state.add(plan['units'][0],{'gate':p.gate_result(rows(),0)})
        state.add(plan['units'][1],{'status':'passed','trace_complete':False})
        self.assertEqual(state.status,'diagnostic_insufficient')
        self.assertEqual(state.pairs['1']['status'],'invalid_missing_post_gate')

if __name__=='__main__': unittest.main()
