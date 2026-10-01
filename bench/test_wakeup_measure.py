import copy
import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('wake',Path(__file__).with_name('wakeup_measure.py'))
wake=importlib.util.module_from_spec(spec);spec.loader.exec_module(wake)


class MeasurementTests(unittest.TestCase):
    def cohort(self):
        return {'kind':'sparse','window':1,'status':'passed','complete_count':8,'start_ms':0,'end_ms':1000,
                'samples':[{'id':str(i),'send_ms':10*i,'accepted_ms':10*i+1,'receive_ms':10*i+3,'settled_ms':10*i+5} for i in range(8)]}
    def test_full_actual_cohort(self):
        result=wake.summarize(self.cohort())
        self.assertEqual(result['send_receive']['count'],8)
        self.assertEqual(result['send_receive']['p95_ms'],3)
        self.assertEqual(result['receive_settled']['p50_ms'],2)
        self.assertEqual(result['completed_per_second'],8)
    def test_missing_or_duplicate_completion_is_failure(self):
        for variant in ('count','missing','duplicate','phase','status'):
            row=self.cohort()
            if variant=='count':row['complete_count']=7
            if variant=='missing':row['samples'].pop()
            if variant=='duplicate':row['samples'][1]['id']='0'
            if variant=='phase':row['samples'][1]['receive_ms']=0
            if variant=='status':row['status']='failed'
            with self.subTest(variant=variant),self.assertRaises(ValueError):wake.summarize(row)
    def test_empty_idle_has_no_fabricated_latency(self):
        row={'kind':'idle','window':1,'status':'passed','complete_count':0,'start_ms':0,'end_ms':5000,'samples':[]}
        self.assertIsNone(wake.summarize(row)['send_receive']['p95_ms'])


if __name__=='__main__':unittest.main()
